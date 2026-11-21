from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from wcp import db, model
from wcp.db import ProtocolError

PROJECT = Path(__file__).resolve().parent.parent


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wcp-db-"))
        self.home = self.tmp / "home"
        self.root = self.tmp / "repo"
        self.root.mkdir()
        self._old_home = os.environ.get("WCP_HOME")
        os.environ["WCP_HOME"] = str(self.home)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("WCP_HOME", None)
        else:
            os.environ["WCP_HOME"] = self._old_home
        shutil.rmtree(self.tmp)

    def insert_task(self, conn, task_id="t-000001", state="proposed"):
        with db.write_txn(conn):
            conn.execute(
                "INSERT INTO tasks(id, goal, state, version, created_at, updated_at) VALUES (?,?,?,0,?,?)",
                (task_id, "goal", state, db.now(), db.now()),
            )
        return model.load_task(conn, task_id)


class DbTests(Base):
    def test_open_db_private_and_idempotent(self):
        conn = db.open_db(self.root)
        d = db.repo_dir(self.root)
        self.assertEqual(d, self.home / "repos" / db.repo_id(self.root))
        for p in (self.home, self.home / "repos", d):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(d / "state.sqlite3").st_mode), 0o600)
        self.assertEqual(db.meta_get(conn, "root"), str(self.root.resolve()))
        self.assertEqual(db.meta_get(conn, "epoch"), "0")
        self.assertEqual(db.meta_get(conn, "link_dirs"), "[]")
        self.assertIsNone(db.meta_get(conn, "nope"))
        self.assertEqual(db.meta_get(conn, "nope", "dflt"), "dflt")
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"meta", "tasks", "worktrees", "task_scopes", "task_deps", "task_checks",
                         "baseline_files", "submissions", "receipts", "scope_requests", "events",
                         "locks"} <= tables)
        conn2 = db.open_db(self.root)  # idempotent
        self.assertEqual(db.meta_get(conn2, "epoch"), "0")
        self.assertEqual(len(db.repo_id(self.root)), 16)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        indexes = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue({"idx_tasks_state", "idx_worktrees_task_status", "idx_task_scopes_kind_task",
                         "idx_events_task", "idx_run_tasks_run_status"} <= indexes)
        conn.close()
        conn2.close()

    def test_open_db_read_only_skips_writes_when_db_exists(self):
        conn = db.open_db(self.root)
        conn.close()
        ro = db.open_db(self.root, read_only=True)
        self.addCleanup(ro.close)
        self.assertEqual(db.meta_get(ro, "epoch"), "0")
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("INSERT INTO meta(key, value) VALUES ('ro_test', '1')")

    def test_default_home_platform_paths(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(db.sys, "platform", "darwin"):
                self.assertEqual(db.DEFAULT_HOME(), Path.home() / "Library" / "Application Support" / "wcp")
            with mock.patch.object(db.sys, "platform", "linux"):
                self.assertEqual(db.DEFAULT_HOME(), Path.home() / ".local" / "share" / "wcp")
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/tmp/xdg-data"}, clear=True):
            with mock.patch.object(db.sys, "platform", "linux"):
                self.assertEqual(db.DEFAULT_HOME(), Path("/tmp/xdg-data/wcp"))

    def test_write_txn_rollback_and_no_nesting(self):
        conn = db.open_db(self.root)
        self.addCleanup(conn.close)
        with self.assertRaises(ValueError):
            with db.write_txn(conn):
                db.meta_set(conn, "k", "v")
                raise ValueError("boom")
        self.assertIsNone(db.meta_get(conn, "k"))
        self.assertFalse(conn.in_transaction)
        with db.write_txn(conn):
            with self.assertRaises(RuntimeError):
                with db.write_txn(conn):
                    pass
        with self.assertRaises(RuntimeError):
            db.next_epoch(conn)  # outside a txn

    def test_epoch_monotonic_across_two_connections(self):
        c1 = db.open_db(self.root)
        c2 = db.open_db(self.root)
        self.addCleanup(c1.close)
        self.addCleanup(c2.close)
        seen = []
        for i in range(6):
            conn = c1 if i % 2 == 0 else c2
            with db.write_txn(conn):
                seen.append(db.next_epoch(conn))
        self.assertEqual(seen, [1, 2, 3, 4, 5, 6])
        self.assertEqual(db.meta_get(c1, "epoch"), "6")
        self.assertEqual(db.meta_get(c2, "epoch"), "6")

    def test_new_id_and_events(self):
        self.assertRegex(db.new_id("t-"), r"^t-[0-9a-f]{6}$")
        conn = db.open_db(self.root)
        self.addCleanup(conn.close)
        self.insert_task(conn)
        with db.write_txn(conn):
            db.add_event(conn, "t-000001", "note", {"b": 1, "a": "x"})
        row = conn.execute("SELECT * FROM events").fetchone()
        self.assertEqual((row["task_id"], row["kind"], row["detail_json"]), ("t-000001", "note", '{"a": "x", "b": 1}'))

    def test_integrator_lock_excludes_across_processes(self):
        child_src = (
            "import sys\nfrom pathlib import Path\nfrom wcp import db\n"
            "with db.integrator_lock(Path(sys.argv[1]), 'child'):\n"
            "    sys.stdout.write('held\\n'); sys.stdout.flush()\n"
            "    sys.stdin.readline()\n"
            "sys.stdout.write('released\\n'); sys.stdout.flush()\n"
        )
        env = dict(os.environ, PYTHONPATH=str(PROJECT))
        child = subprocess.Popen(
            [sys.executable, "-c", child_src, str(self.root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, cwd=str(PROJECT), text=True,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "held")
            with self.assertRaises(ProtocolError) as cm:
                with db.integrator_lock(self.root, "parent"):
                    pass
            self.assertEqual(cm.exception.code, "lock_held")
            self.assertEqual(cm.exception.detail.get("holder"), "child")
            conn = db.open_db(self.root)
            self.addCleanup(conn.close)
            self.assertEqual(conn.execute("SELECT holder FROM locks WHERE name='integrator'").fetchone()["holder"], "child")
            child.stdin.write("\n")
            child.stdin.flush()
            self.assertEqual(child.stdout.readline().strip(), "released")
            self.assertEqual(child.wait(timeout=30), 0)
        finally:
            if child.poll() is None:
                child.kill()
            child.stdin.close()
            child.stdout.close()
        with db.integrator_lock(self.root, "parent"):
            self.assertEqual(conn.execute("SELECT holder FROM locks WHERE name='integrator'").fetchone()["holder"], "parent")
        self.assertIsNone(conn.execute("SELECT holder FROM locks WHERE name='integrator'").fetchone())


class SchemaV2Tests(Base):
    V2_TABLES = ("rebase_obligations", "runs", "run_tasks", "run_attempts")

    def test_migration_from_v1_adds_tables_and_keeps_rows(self):
        conn = db.open_db(self.root)
        self.insert_task(conn)
        with db.write_txn(conn):
            db.add_event(conn, "t-000001", "note", {"k": 1})
            db.meta_set(conn, "epoch", "7")
        for t in self.V2_TABLES:  # rewind to exactly the version-1 schema
            conn.execute("DROP TABLE %s" % t)
        with db.write_txn(conn):
            db.meta_set(conn, "schema_version", "1")
        conn.close()
        conn = db.open_db(self.root)
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(set(self.V2_TABLES) <= tables)
        self.assertEqual(db.meta_get(conn, "schema_version"), "2")
        self.assertEqual(db.SCHEMA_VERSION, "2")
        self.assertEqual(db.meta_get(conn, "epoch"), "7")  # existing rows untouched
        self.assertEqual(model.load_task(conn, "t-000001").state, "proposed")
        self.assertEqual(conn.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):  # the status CHECK is real
            with db.write_txn(conn):
                conn.execute("INSERT INTO rebase_obligations VALUES ('t-000001', 1, 'x', 'bogus', NULL)")
        with self.assertRaises(sqlite3.IntegrityError):  # one obligation per (task, epoch, path)
            with db.write_txn(conn):
                conn.execute("INSERT INTO rebase_obligations VALUES ('t-000001', 1, 'x', 'pending', NULL)")
                conn.execute("INSERT INTO rebase_obligations VALUES ('t-000001', 1, 'x', 'kept', NULL)")
        conn.close()

    def test_sleep_seam_and_run_dir(self):
        self.assertIs(db.sleep, time.sleep)
        d = db.run_dir(self.root, "r-abc123")
        self.assertEqual(d, db.repo_dir(self.root) / "runs" / "r-abc123")
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(d.parent).st_mode), 0o700)
        self.assertEqual(db.run_dir(self.root, "r-abc123"), d)  # idempotent


class ModelTests(Base):
    def test_run_and_obligation_loaders(self):
        conn = db.open_db(self.root)
        t = self.insert_task(conn)
        with db.write_txn(conn):
            conn.execute("INSERT INTO runs VALUES ('r-1', 'sha', '{}', '/root', 1.0, 2.0, 'running')")
            conn.executemany("INSERT INTO run_tasks VALUES (?,?,?,?,?,?,?,?)",
                             [("r-1", "zeta", t.id, "claude", 1, 0, "building", None),
                              ("r-1", "alpha", t.id, "codex", 2, 1, "paused", "builder_failed")])
            conn.executemany("INSERT INTO run_attempts VALUES (?,?,?,?,?,?,?,?,?)",
                             [("r-1", "zeta", 2, 5, "/log2", None, 2.0, None, None),
                              ("r-1", "zeta", 1, 4, "/log1", 123, 1.0, 1.5, 1)])
            conn.executemany("INSERT INTO rebase_obligations VALUES (?,?,?,?,?)",
                             [(t.id, 2, "src/b.py", "pending", "/old"), (t.id, 2, "src/a.py", "kept", "/old"),
                              (t.id, 1, "src/c.py", "resolved", "/older")])
        run = model.load_run(conn, "r-1")
        self.assertEqual((run.id, run.status, run.plan_sha256), ("r-1", "running", "sha"))
        with self.assertRaises(ProtocolError) as cm:
            model.load_run(conn, "r-none")
        self.assertEqual(cm.exception.code, "not_found")
        rts = model.load_run_tasks(conn, "r-1")
        self.assertEqual([(r.key, r.builder, r.attempts, r.rebases, r.status, r.last_error) for r in rts],
                         [("zeta", "claude", 1, 0, "building", None), ("alpha", "codex", 2, 1, "paused", "builder_failed")])
        atts = model.load_run_attempts(conn, "r-1", "zeta")
        self.assertEqual([(a.attempt, a.task_epoch, a.pgid, a.exit_code) for a in atts], [(1, 4, 123, 1), (2, 5, None, None)])
        self.assertEqual(model.load_run_attempts(conn, "r-1", "nope"), [])
        obs = model.load_obligations(conn, t.id, 2)
        self.assertEqual([(o.path, o.status, o.retired_worktree) for o in obs],
                         [("src/a.py", "kept", "/old"), ("src/b.py", "pending", "/old")])
        self.assertEqual([o.path for o in model.load_obligations(conn, t.id, 1)], ["src/c.py"])
        self.assertEqual(model.load_obligations(conn, t.id, None), [])
        conn.close()

    def test_state_sets(self):
        self.assertEqual(model.ACTIVE_STATES, {"provisioning", "claimed", "running", "blocked", "conflicted",
                                              "submitted", "verifying", "publishing"})
        self.assertEqual(model.CLAIMABLE_EXPIRED, {"provisioning", "claimed", "running", "blocked",
                                                  "conflicted", "submitted"})
        self.assertEqual(model.TERMINAL_STATES, {"accepted", "abandoned", "cancelled"})
        for pair in [("proposed", "ready"), ("ready", "provisioning"), ("provisioning", "claimed"),
                     ("provisioning", "ready"), ("running", "submitted"), ("submitted", "verifying"),
                     ("verifying", "publishing"), ("publishing", "accepted"), ("publishing", "conflicted"),
                     ("verifying", "conflicted"), ("conflicted", "running"), ("submitted", "running"),
                     ("verifying", "running"), ("running", "cancelled"), ("blocked", "provisioning"),
                     ("submitted", "provisioning"), ("proposed", "cancelled")]:
            self.assertIn(pair, model.TRANSITIONS, pair)
        for pair in [("verifying", "cancelled"), ("publishing", "cancelled"), ("accepted", "cancelled"),
                     ("cancelled", "cancelled"), ("verifying", "provisioning"), ("publishing", "provisioning"),
                     ("proposed", "running"), ("accepted", "running"), ("ready", "claimed")]:
            self.assertNotIn(pair, model.TRANSITIONS, pair)

    def test_transition_and_cas(self):
        conn = db.open_db(self.root)
        self.addCleanup(conn.close)
        t = self.insert_task(conn)
        self.assertEqual((t.state, t.version), ("proposed", 0))
        with db.write_txn(conn):
            t2 = model.transition(conn, t, "ready")
        self.assertEqual((t2.state, t2.version), ("ready", 1))
        self.assertGreaterEqual(t2.updated_at, t.updated_at)
        ev = conn.execute("SELECT kind, detail_json FROM events WHERE task_id=?", (t.id,)).fetchall()
        self.assertEqual(ev[0]["kind"], "state")
        self.assertIn('"from": "proposed"', ev[0]["detail_json"])
        self.assertIn('"to": "ready"', ev[0]["detail_json"])
        # stale object (version 0) is refused: implicit CAS
        with self.assertRaises(ProtocolError) as cm:
            with db.write_txn(conn):
                model.transition(conn, t, "provisioning")
        self.assertEqual(cm.exception.code, "stale_version")
        # explicit --expect-version mismatch
        with self.assertRaises(ProtocolError) as cm:
            with db.write_txn(conn):
                model.transition(conn, t2, "provisioning", expect_version=7)
        self.assertEqual(cm.exception.code, "stale_version")
        # illegal transition
        with self.assertRaises(ProtocolError) as cm:
            with db.write_txn(conn):
                model.transition(conn, t2, "running")
        self.assertEqual(cm.exception.code, "bad_state")
        self.assertEqual(model.load_task(conn, t.id).version, 1)  # refusals rolled back
        with db.write_txn(conn):
            t3 = model.transition(conn, t2, "provisioning", expect_version=1, owner="o", epoch=3,
                                  lease_expires_at=123.0, worktree_path="/wt")
        self.assertEqual((t3.state, t3.version, t3.owner, t3.epoch, t3.worktree_path), ("provisioning", 2, "o", 3, "/wt"))
        self.assertTrue(t3.lease_expired(123.0))
        self.assertFalse(t3.lease_expired(122.9))
        with self.assertRaises(TypeError):
            with db.write_txn(conn):
                model.transition(conn, t3, "claimed", goal="nope")
        with self.assertRaises(RuntimeError):
            model.transition(conn, t3, "claimed")  # outside write_txn

    def test_cancel_refused_from_verifying(self):
        conn = db.open_db(self.root)
        self.addCleanup(conn.close)
        t = self.insert_task(conn, state="verifying")
        with self.assertRaises(ProtocolError) as cm:
            with db.write_txn(conn):
                model.transition(conn, t, "cancelled")
        self.assertEqual(cm.exception.code, "bad_state")

    def test_loaders(self):
        conn = db.open_db(self.root)
        self.addCleanup(conn.close)
        t = self.insert_task(conn)
        with db.write_txn(conn):
            conn.executemany("INSERT INTO task_scopes VALUES (?,?,?)",
                             [(t.id, "path", "src/**"), (t.id, "resource", "port:5199"), (t.id, "path", "docs/*.md")])
            conn.executemany("INSERT INTO task_deps VALUES (?,?)", [(t.id, "t-b"), (t.id, "t-a")])
            conn.executemany("INSERT INTO task_checks VALUES (?,?,?)",
                             [(t.id, 1, '{"argv": ["python3", "x.py"], "cmd": null, "cwd": ".", "timeout_s": 5}'),
                              (t.id, 0, "python3 tests/check.py")])
            conn.executemany("INSERT INTO worktrees VALUES (?,?,?,?,?)",
                             [("/wt2", t.id, 2, "active", 1.0), ("/wt1", t.id, 1, "retired", 0.0)])
        self.assertEqual(model.load_scopes(conn, t.id), {"path": ["docs/*.md", "src/**"], "resource": ["port:5199"]})
        self.assertEqual(model.load_deps(conn, t.id), ["t-a", "t-b"])
        self.assertEqual(model.load_checks(conn, t.id), [
            {"argv": None, "cmd": "python3 tests/check.py", "cwd": ".", "timeout_s": 900},
            {"argv": ["python3", "x.py"], "cmd": None, "cwd": ".", "timeout_s": 5}])
        for valid_cwd, expected_cwd in [(None, "."), ("", "."), (".", "."), ("src/sub", "src/sub")]:
            with db.write_txn(conn):
                conn.execute("DELETE FROM task_checks WHERE task_id=?", (t.id,))
                conn.execute("INSERT INTO task_checks VALUES (?, 0, ?)",
                             (t.id, '{"cmd": "true", "cwd": %s}' % (
                                 "null" if valid_cwd is None else '"%s"' % valid_cwd)))
            self.assertEqual(model.load_checks(conn, t.id)[0]["cwd"], expected_cwd)
        for bad_cwd in ['".."', '"../outside"', '"src/../../outside"', '"/tmp"', '".git"', '123']:
            with db.write_txn(conn):
                conn.execute("DELETE FROM task_checks WHERE task_id=?", (t.id,))
                conn.execute("INSERT INTO task_checks VALUES (?, 0, ?)",
                             (t.id, '{"cmd": "true", "cwd": %s}' % bad_cwd))
            with self.assertRaises(ProtocolError) as cm:
                model.load_checks(conn, t.id)
            self.assertEqual(cm.exception.code, "unsafe_path")
        wts = model.load_worktrees(conn, t.id)
        self.assertEqual([(w.path, w.epoch, w.status) for w in wts], [("/wt1", 1, "retired"), ("/wt2", 2, "active")])
        self.assertEqual(model.load_scopes(conn, "t-none"), {"path": [], "resource": []})
        with self.assertRaises(ProtocolError) as cm:
            model.load_task(conn, "t-none")
        self.assertEqual(cm.exception.code, "not_found")
        with db.write_txn(conn):
            conn.execute("INSERT INTO submissions VALUES (?,?,?,?,?,?,?)",
                         ("s-1", t.id, 2, "/wt2", 1.0, "/b", '{"changes": []}'))
        sub = model.Submission.from_row(conn.execute("SELECT * FROM submissions").fetchone())
        self.assertEqual((sub.id, sub.manifest()), ("s-1", {"changes": []}))


if __name__ == "__main__":
    unittest.main()
