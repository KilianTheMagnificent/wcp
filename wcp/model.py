"""Dataclasses for the durable records and the task state machine."""
from __future__ import annotations

import json
from dataclasses import dataclass, fields

from . import db, paths
from .db import ProtocolError

STATES = (
    "proposed", "ready", "provisioning", "claimed", "running", "blocked", "submitted",
    "verifying", "publishing", "conflicted", "accepted", "abandoned", "cancelled",
)
TERMINAL_STATES = frozenset({"accepted", "abandoned", "cancelled"})
# Scope is reserved in these states regardless of lease (spec "Scope semantics").
ACTIVE_STATES = frozenset({
    "provisioning", "claimed", "running", "blocked", "conflicted", "submitted", "verifying", "publishing",
})
# Reclaimable once the lease has expired; never verifying/publishing.
CLAIMABLE_EXPIRED = frozenset({"provisioning", "claimed", "running", "blocked", "conflicted", "submitted"})

TRANSITIONS = {
    ("proposed", "ready"),
    ("ready", "provisioning"),
    ("provisioning", "claimed"),
    ("provisioning", "ready"),
    ("claimed", "running"),
    ("claimed", "submitted"),
    ("claimed", "blocked"),
    ("claimed", "abandoned"),
    ("running", "submitted"),
    ("running", "blocked"),
    ("running", "abandoned"),
    ("blocked", "running"),
    ("blocked", "abandoned"),
    ("submitted", "verifying"),
    ("submitted", "running"),
    ("verifying", "publishing"),
    ("verifying", "running"),
    ("verifying", "conflicted"),
    ("conflicted", "running"),
    ("conflicted", "abandoned"),
    ("publishing", "accepted"),
    ("publishing", "conflicted"),
}
# reclaim / re-provision after an expired lease
TRANSITIONS |= {(s, "provisioning") for s in CLAIMABLE_EXPIRED}
# coordinator cancel
TRANSITIONS |= {(s, "cancelled") for s in STATES if s not in ("verifying", "publishing", "accepted", "cancelled")}
TRANSITIONS = frozenset(TRANSITIONS)

_UPDATABLE = ("owner", "epoch", "lease_expires_at", "base_revision", "worktree_path", "blocked_reason")


class _Row:
    @classmethod
    def from_row(cls, row):
        return cls(*[row[f.name] for f in fields(cls)])


@dataclass
class Task(_Row):
    id: str
    goal: str
    state: str
    version: int
    owner: str | None
    epoch: int | None
    lease_expires_at: float | None
    base_revision: str | None
    worktree_path: str | None
    blocked_reason: str | None
    created_at: float
    updated_at: float

    def lease_expired(self, at: float) -> bool:
        return self.lease_expires_at is None or at >= self.lease_expires_at


@dataclass
class Submission(_Row):
    id: str
    task_id: str
    epoch: int
    worktree_path: str
    created_at: float
    bundle_dir: str
    manifest_json: str

    def manifest(self) -> dict:
        return json.loads(self.manifest_json)


@dataclass
class Receipt(_Row):
    id: int
    task_id: str
    submission_id: str | None
    phase: str
    cmd: str
    exit_code: int
    duration_ms: int
    output_tail: str
    created_at: float


@dataclass
class ScopeRequest(_Row):
    id: int
    task_id: str
    kind: str
    pattern: str
    reason: str
    status: str
    created_at: float


@dataclass
class Worktree(_Row):
    path: str
    task_id: str
    epoch: int
    status: str
    created_at: float


@dataclass
class RebaseObligation(_Row):
    task_id: str
    epoch: int
    path: str
    status: str  # pending | resolved | kept
    retired_worktree: str | None


@dataclass
class Run(_Row):
    id: str
    plan_sha256: str
    plan_json: str
    root: str
    created_at: float
    updated_at: float
    status: str


@dataclass
class RunTask(_Row):
    run_id: str
    key: str
    task_id: str
    builder: str
    attempts: int
    rebases: int
    status: str
    last_error: str | None


@dataclass
class RunAttempt(_Row):
    run_id: str
    key: str
    attempt: int
    task_epoch: int | None
    log_path: str | None
    pgid: int | None
    started_at: float | None
    exited_at: float | None
    exit_code: int | None


def load_task(conn, task_id: str) -> Task:
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise ProtocolError("not_found", "no task %s" % task_id, {"task_id": task_id})
    return Task.from_row(row)


def load_scopes(conn, task_id: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"path": [], "resource": []}
    for row in conn.execute(
        "SELECT kind, pattern FROM task_scopes WHERE task_id=? ORDER BY kind, pattern", (task_id,)
    ):
        out[row["kind"]].append(row["pattern"])
    return out


def _check_cwd(raw) -> str:
    if raw is None or raw in ("", "."):
        return "."
    if not isinstance(raw, str):
        raise ProtocolError("unsafe_path", "unsafe path %r" % (raw,), {"path": raw})
    return paths.normalize_repo_path(raw)


def load_checks(conn, task_id: str) -> list[dict]:
    out = []
    for row in conn.execute("SELECT cmd FROM task_checks WHERE task_id=? ORDER BY ord", (task_id,)):
        text = row["cmd"]
        check = None
        if text.lstrip().startswith("{"):
            try:
                check = json.loads(text)
            except ValueError:
                check = None
        if not isinstance(check, dict):
            check = {"argv": None, "cmd": text, "cwd": ".", "timeout_s": 900}
        else:
            check["cwd"] = _check_cwd(check.get("cwd"))
        out.append(check)
    return out


def load_deps(conn, task_id: str) -> list[str]:
    return [r["depends_on"] for r in conn.execute(
        "SELECT depends_on FROM task_deps WHERE task_id=? ORDER BY depends_on", (task_id,))]


def load_worktrees(conn, task_id: str) -> list[Worktree]:
    return [Worktree.from_row(r) for r in conn.execute(
        "SELECT * FROM worktrees WHERE task_id=? ORDER BY epoch, path", (task_id,))]


def load_obligations(conn, task_id: str, epoch) -> list[RebaseObligation]:
    return [RebaseObligation.from_row(r) for r in conn.execute(
        "SELECT * FROM rebase_obligations WHERE task_id=? AND epoch=? ORDER BY path", (task_id, epoch))]


def load_run(conn, run_id: str) -> Run:
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise ProtocolError("not_found", "no run %s" % run_id, {"run_id": run_id})
    return Run.from_row(row)


def load_run_tasks(conn, run_id: str) -> list[RunTask]:
    return [RunTask.from_row(r) for r in conn.execute(
        "SELECT * FROM run_tasks WHERE run_id=? ORDER BY rowid", (run_id,))]  # plan order


def load_run_attempts(conn, run_id: str, key: str) -> list[RunAttempt]:
    return [RunAttempt.from_row(r) for r in conn.execute(
        "SELECT * FROM run_attempts WHERE run_id=? AND key=? ORDER BY attempt", (run_id, key))]


def transition(conn, task: Task, to_state: str, *, expect_version: int | None = None, **updates) -> Task:
    """Move `task` to `to_state` inside the caller's write_txn.

    Re-reads the row; the version the caller saw (and --expect-version, if given) must still hold.
    """
    if not conn.in_transaction:
        raise RuntimeError("transition requires write_txn")
    bad = set(updates) - set(_UPDATABLE)
    if bad:
        raise TypeError("transition: unknown columns %s" % sorted(bad))
    cur = load_task(conn, task.id)
    for expected in (expect_version, task.version):
        if expected is not None and cur.version != expected:
            raise ProtocolError("stale_version", "task %s is at version %d, expected %d" % (
                task.id, cur.version, expected), {"task_id": task.id, "actual": cur.version, "expected": expected})
    if (cur.state, to_state) not in TRANSITIONS:
        raise ProtocolError("bad_state", "task %s cannot go %s -> %s" % (task.id, cur.state, to_state),
                            {"task_id": task.id, "from": cur.state, "to": to_state})
    new_version = cur.version + 1
    cols = ["state", "version", "updated_at"] + list(updates)
    vals = [to_state, new_version, db.now()] + list(updates.values())
    res = conn.execute(
        "UPDATE tasks SET %s WHERE id=? AND version=?" % ", ".join(c + "=?" for c in cols),
        vals + [task.id, cur.version],
    )
    if res.rowcount != 1:
        raise ProtocolError("stale_version", "task %s changed underneath" % task.id, {"task_id": task.id})
    detail = {"from": cur.state, "to": to_state, "version": new_version}
    detail.update(updates)
    db.add_event(conn, task.id, "state", detail)
    return load_task(conn, task.id)
