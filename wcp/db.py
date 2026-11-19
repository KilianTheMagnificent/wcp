"""SQLite state: open/migrate, BEGIN IMMEDIATE helper, meta/epoch, integrator flock."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = "2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tasks(
    id TEXT PRIMARY KEY, goal TEXT, state TEXT NOT NULL, version INT NOT NULL DEFAULT 0,
    owner TEXT, epoch INT, lease_expires_at REAL, base_revision TEXT, worktree_path TEXT,
    blocked_reason TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS worktrees(
    path TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id), epoch INT,
    status TEXT CHECK(status IN ('provisioning','active','retired')), created_at REAL);
CREATE TABLE IF NOT EXISTS task_scopes(
    task_id TEXT REFERENCES tasks(id), kind TEXT CHECK(kind IN ('path','resource')), pattern TEXT,
    PRIMARY KEY(task_id, kind, pattern));
CREATE TABLE IF NOT EXISTS task_deps(
    task_id TEXT REFERENCES tasks(id), depends_on TEXT, PRIMARY KEY(task_id, depends_on));
CREATE TABLE IF NOT EXISTS task_checks(
    task_id TEXT REFERENCES tasks(id), ord INT, cmd TEXT, PRIMARY KEY(task_id, ord));
CREATE TABLE IF NOT EXISTS baseline_files(
    worktree_path TEXT, path TEXT, sha256 TEXT, mode TEXT, PRIMARY KEY(worktree_path, path));
CREATE TABLE IF NOT EXISTS submissions(
    id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id), epoch INT, worktree_path TEXT,
    created_at REAL, bundle_dir TEXT, manifest_json TEXT);
CREATE TABLE IF NOT EXISTS receipts(
    id INTEGER PRIMARY KEY, task_id TEXT REFERENCES tasks(id), submission_id TEXT,
    phase TEXT CHECK(phase IN ('submit','verify')), cmd TEXT, exit_code INT, duration_ms INT,
    output_tail TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS scope_requests(
    id INTEGER PRIMARY KEY, task_id TEXT REFERENCES tasks(id), kind TEXT, pattern TEXT,
    reason TEXT, status TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY, task_id TEXT REFERENCES tasks(id), at REAL, kind TEXT, detail_json TEXT);
CREATE TABLE IF NOT EXISTS locks(name TEXT PRIMARY KEY, holder TEXT, acquired_at REAL);
-- schema 2 (addendum): rebase obligations + `wcp run` state; CREATE IF NOT EXISTS is the whole migration
CREATE TABLE IF NOT EXISTS rebase_obligations(
    task_id TEXT REFERENCES tasks(id), epoch INT, path TEXT,
    status TEXT CHECK(status IN ('pending','resolved','kept')), retired_worktree TEXT,
    PRIMARY KEY(task_id, epoch, path));
CREATE TABLE IF NOT EXISTS runs(
    id TEXT PRIMARY KEY, plan_sha256 TEXT, plan_json TEXT, root TEXT, created_at REAL, updated_at REAL,
    status TEXT);
CREATE TABLE IF NOT EXISTS run_tasks(
    run_id TEXT, key TEXT, task_id TEXT, builder TEXT, attempts INT, rebases INT, status TEXT, last_error TEXT,
    PRIMARY KEY(run_id, key));
CREATE TABLE IF NOT EXISTS run_attempts(
    run_id TEXT, key TEXT, attempt INT, task_epoch INT, log_path TEXT, pgid INT, started_at REAL,
    exited_at REAL, exit_code INT, PRIMARY KEY(run_id, key, attempt));
"""


class ProtocolError(Exception):
    """Typed protocol refusal; the CLI maps it to exit code 3."""

    def __init__(self, code: str, message: str | None = None, detail: dict | None = None):
        self.code = code
        self.message = message or code
        self.detail = detail or {}
        super().__init__(self.message)


def now() -> float:
    return time.time()


sleep = time.sleep  # seam: --wait polling calls db.sleep(2); tests monkeypatch it beside db.now


def new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(3)


def DEFAULT_HOME() -> Path:
    env = os.environ.get("WCP_HOME")
    if env:
        return Path(env)
    return Path.home() / "Library" / "Application Support" / "wcp"


def ensure_private(path: Path, is_dir: bool) -> None:
    if is_dir:
        missing = []
        p = path
        while not p.exists():
            missing.append(p)
            p = p.parent
        for d in reversed(missing):
            try:
                os.mkdir(d, 0o700)
            except FileExistsError:
                pass
            os.chmod(d, 0o700)
        os.chmod(path, 0o700)
    else:
        os.chmod(path, 0o600)


def repo_id(root: Path) -> str:
    return hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:16]


def repo_dir(root: Path) -> Path:
    d = DEFAULT_HOME() / "repos" / repo_id(root)
    ensure_private(d, True)
    return d


def open_db(root: Path) -> sqlite3.Connection:
    d = repo_dir(root)
    db_path = d / "state.sqlite3"
    conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    ensure_private(db_path, False)
    with write_txn(conn):
        meta_set(conn, "schema_version", SCHEMA_VERSION)
        for key, value in (
            ("root", str(Path(root).resolve())),
            ("epoch", "0"),
            ("required_checks", "[]"),
            ("link_dirs", "[]"),
            ("preserve_roots", "[]"),
        ):
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (key, value))
    return conn


def run_dir(root: Path, run_id: str) -> Path:
    d = repo_dir(root) / "runs" / run_id
    ensure_private(d, True)
    return d


@contextmanager
def write_txn(conn: sqlite3.Connection):
    if conn.in_transaction:
        raise RuntimeError("write_txn cannot be nested")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def meta_get(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return default if row is None else row["value"]


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))


def next_epoch(conn: sqlite3.Connection) -> int:
    if not conn.in_transaction:
        raise RuntimeError("next_epoch requires write_txn")
    epoch = int(meta_get(conn, "epoch", "0")) + 1
    meta_set(conn, "epoch", str(epoch))
    return epoch


def add_event(conn: sqlite3.Connection, task_id: str, kind: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO events(task_id, at, kind, detail_json) VALUES (?, ?, ?, ?)",
        (task_id, now(), kind, json.dumps(detail, sort_keys=True, default=str)),
    )


@contextmanager
def integrator_lock(root: Path, holder: str):
    lock_path = repo_dir(root) / "integrator.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            conn = open_db(root)
            try:
                row = conn.execute("SELECT holder, acquired_at FROM locks WHERE name='integrator'").fetchone()
            finally:
                conn.close()
            detail = {"lock": str(lock_path)}
            if row is not None:
                detail.update({"holder": row["holder"], "acquired_at": row["acquired_at"]})
            raise ProtocolError("lock_held", "another integrator holds the lock", detail)
        conn = open_db(root)
        try:
            with write_txn(conn):
                conn.execute(
                    "INSERT OR REPLACE INTO locks(name, holder, acquired_at) VALUES ('integrator', ?, ?)",
                    (holder, now()),
                )
            try:
                yield
            finally:
                with write_txn(conn):
                    conn.execute("DELETE FROM locks WHERE name='integrator'")
        finally:
            conn.close()
    finally:
        os.close(fd)  # closing the descriptor releases the flock; the kernel does the same if we die
