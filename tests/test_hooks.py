from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wcp import cli, db, hooks

SECRET_MARKER = "TOOL-CONTENT-MUST-NOT-LEAK"


def claude(tool, path, cwd, key="file_path"):
    return {"hook_event_name": "PreToolUse", "session_id": "s", "tool_name": tool,
            "tool_input": {key: path, "content": SECRET_MARKER}, "cwd": cwd}


def grok(path, cwd):
    return {"hookEventName": "pre_tool_use", "sessionId": "s", "toolName": "write_file",
            "toolInput": {"filePath": path, "content": SECRET_MARKER}, "cwd": cwd, "workspaceRoot": cwd}


def codex(lines, cwd):
    patch = "*** Begin Patch\n" + "".join(l + "\n" for l in lines) + "*** End Patch\n"
    return {"hook_event_name": "PreToolUse", "tool_name": "apply_patch",
            "tool_input": {"patch": patch}, "cwd": cwd}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wcp-hooks-")).resolve()
        self.home = self.tmp / "home"
        self.root = self.tmp / "repo"
        self.other = self.tmp / "other"
        (self.root / "src").mkdir(parents=True)
        self.other.mkdir()
        self._old_home = os.environ.get("WCP_HOME")
        os.environ["WCP_HOME"] = str(self.home)
        self.conn = db.open_db(self.root)
        self.wt_dir = db.repo_dir(self.root) / "worktrees"

    def tearDown(self):
        self.conn.close()
        if self._old_home is None:
            os.environ.pop("WCP_HOME", None)
        else:
            os.environ["WCP_HOME"] = self._old_home
        shutil.rmtree(self.tmp)

    def add_task(self, tid="t-aaaaaa", state="running", epoch=1, lease=3600.0, write=("src/**",)):
        with db.write_txn(self.conn):
            self.conn.execute(
                "INSERT INTO tasks(id, goal, state, version, owner, epoch, lease_expires_at, created_at, updated_at)"
                " VALUES (?,?,?,1,?,?,?,?,?)",
                (tid, "goal", state, "claude:x", epoch, db.now() + lease if lease is not None else None,
                 db.now(), db.now()))
            for pat in write:
                self.conn.execute("INSERT INTO task_scopes(task_id, kind, pattern) VALUES (?,?,?)",
                                  (tid, "path", pat))
        return tid

    def add_worktree(self, tid="t-aaaaaa", epoch=1, status="active"):
        path = self.wt_dir / ("%s-e%d" % (tid, epoch))
        (path / "src").mkdir(parents=True, exist_ok=True)
        with db.write_txn(self.conn):
            self.conn.execute("INSERT INTO worktrees(path, task_id, epoch, status, created_at) VALUES (?,?,?,?,?)",
                              (str(path), tid, epoch, status, db.now()))
        return path

    def decide(self, payload, env=None):
        return hooks.decide(payload, self.home, {} if env is None else env)

    def assertDeny(self, payload, *needles, env=None):
        allow, reason = self.decide(payload, env)
        self.assertFalse(allow, reason)
        self.assertTrue(reason.startswith("wcp: "), reason)
        self.assertNotIn("\n", reason)
        self.assertNotIn(SECRET_MARKER, reason)
        for n in needles:
            self.assertIn(n, reason)
        return reason

    def assertAllow(self, payload, env=None):
        allow, reason = self.decide(payload, env)
        self.assertTrue(allow, reason)


class ExtractNormalizeTests(unittest.TestCase):
    def test_extract_known_keys(self):
        self.assertEqual(hooks.extract_paths({"file_path": "/a", "content": "x"}), ["/a"])
        self.assertEqual(hooks.extract_paths({"notebook_path": "/n.ipynb", "new_source": "x"}), ["/n.ipynb"])
        self.assertEqual(hooks.extract_paths({"path": "rel/p"}), ["rel/p"])
        self.assertEqual(hooks.extract_paths({"filePath": "/g", "content": "x"}), ["/g"])
        self.assertEqual(hooks.extract_paths({"command": "ls", "file_path": ""}), [])
        self.assertEqual(hooks.extract_paths({}), [])

    def test_extract_apply_patch_lines(self):
        patch = ("*** Begin Patch\n*** Update File: src/b.py\n@@\n-x\n+y\n*** Add File: new/f.py\n+1\n"
                 "*** Delete File: old.py\n*** Update File: src/c.py\n*** Move to: src/d.py  \n*** End Patch\n")
        self.assertEqual(hooks.extract_paths({"patch": patch}),
                         ["src/b.py", "new/f.py", "old.py", "src/c.py", "src/d.py"])
        # dedupe keeps first occurrence
        self.assertEqual(hooks.extract_paths({"file_path": "a", "x": "*** Update File: a\n"}), ["a"])

    def test_patch_lines_come_only_from_apply_patch_or_a_patch_field(self):
        body = "*** Update File: src/b.py\n"
        for tool, field in (("Write", "content"), ("Edit", "new_string"), ("Edit", "old_string"),
                            ("NotebookEdit", "new_source"), ("Write", "description")):
            with self.subTest(tool=tool, field=field):
                self.assertEqual(hooks.extract_paths({"file_path": "/a", field: body}, tool), ["/a"])
        self.assertEqual(hooks.extract_paths({"content": body}, "Write"), [])
        self.assertEqual(hooks.extract_paths({"patch": body}, "Write"), ["src/b.py"])
        self.assertEqual(hooks.extract_paths({"input": body}, "codex"), ["src/b.py"])
        self.assertEqual(hooks.extract_paths({"anything": body}, "apply_patch"), ["src/b.py"])
        self.assertEqual(hooks.extract_paths({"anything": body}, "APPLY_PATCH"), ["src/b.py"])
        self.assertEqual(hooks.extract_paths({"content": body}, "apply_patch"), [])  # bodies are never scanned

    def test_normalize_dialects(self):
        n = hooks.normalize({"tool_name": "Write", "tool_input": {"file_path": "/a"}, "cwd": "/c"})
        self.assertEqual(n, {"tool_name": "Write", "tool_input": {"file_path": "/a"}, "cwd": "/c"})
        n = hooks.normalize({"toolName": "write_file", "toolInput": {"filePath": "/a"}, "workspaceRoot": "/w"})
        self.assertEqual(n, {"tool_name": "write_file", "tool_input": {"filePath": "/a"}, "cwd": "/w"})
        n = hooks.normalize({"tool_name": "apply_patch", "tool_input": "*** Update File: x\n", "cwd": "/c"})
        self.assertEqual(n["tool_input"], {"patch": "*** Update File: x\n"})
        self.assertEqual(hooks.normalize({"tool_input": 7, "cwd": "/c"})["tool_input"], {})


class DecideTests(Base):
    def test_no_path_allows(self):
        self.add_task()
        self.assertAllow({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}, "cwd": str(self.root)})
        self.assertAllow({"toolName": "run_terminal_command", "toolInput": {}, "cwd": str(self.root)})

    def test_unregistered_path_allows(self):
        self.add_task()
        self.add_worktree()
        self.assertAllow(claude("Write", str(self.other / "x.py"), str(self.other)))
        self.assertAllow(grok(str(self.other / "x.py"), str(self.other)))
        self.assertAllow(codex(["*** Update File: x.py"], str(self.other)))

    def test_no_registry_at_all_allows(self):
        shutil.rmtree(self.home)
        self.assertAllow(claude("Write", str(self.root / "src/a.py"), str(self.root)))

    def test_active_worktree_in_scope_allows(self):
        self.add_task()
        wt = self.add_worktree()
        self.assertAllow(claude("Write", str(wt / "src/a.py"), str(wt)))
        self.assertAllow(claude("Edit", str(wt / "src/deep/b.py"), str(self.other)))
        self.assertAllow(claude("NotebookEdit", str(wt / "src/n.ipynb"), str(wt), key="notebook_path"))
        self.assertAllow(grok(str(wt / "src/a.py"), str(wt)))
        self.assertAllow(codex(["*** Update File: src/a.py", "*** Add File: src/new.py"], str(wt)))
        self.assertAllow(claude("Write", "src/a.py", str(wt)))  # relative to cwd
        self.assertAllow(claude("Write", str(wt / "SRC/A.PY"), str(wt)))  # casefold alias in scope
        self.assertFalse((self.home / "hook-errors.log").exists())

    def test_out_of_scope_denies_naming_scope(self):
        self.add_task(write=("src/engine/**", "docs/*.md"))
        wt = self.add_worktree()
        self.assertDeny(claude("Write", str(wt / "src/other.py"), str(wt)),
                        "src/other.py", "t-aaaaaa", "scope [docs/*.md, src/engine/**]", "wcp scope request")
        self.assertDeny(grok(str(wt / "README.md"), str(wt)), "README.md", "scope")
        self.assertDeny(codex(["*** Update File: src/engine/ok.py", "*** Update File: src/nope.py"], str(wt)),
                        "src/nope.py", "scope")
        self.assertAllow(codex(["*** Update File: src/engine/ok.py", "*** Add File: docs/x.md"], str(wt)))

    def test_expired_lease_denies(self):
        self.add_task(lease=-60.0)
        wt = self.add_worktree()
        self.assertDeny(claude("Write", str(wt / "src/a.py"), str(wt)), "lease for t-aaaaaa expired",
                        "wcp task recover t-aaaaaa")
        self.assertDeny(grok(str(wt / "src/a.py"), str(wt)), "lease")

    def test_null_lease_denies(self):
        self.add_task(lease=None)
        wt = self.add_worktree()
        self.assertDeny(claude("Write", str(wt / "src/a.py"), str(wt)), "lease")

    def test_non_writable_state_denies(self):
        self.add_task(state="submitted")
        wt = self.add_worktree()
        self.assertDeny(claude("Write", str(wt / "src/a.py"), str(wt)), "submitted", "t-aaaaaa")

    def test_provisioning_worktree_denies(self):
        self.add_task(state="provisioning")
        wt = self.add_worktree(status="provisioning")
        self.assertDeny(claude("Write", str(wt / "src/a.py"), str(wt)), "provisioning", "t-aaaaaa")

    def test_retired_worktree_denies(self):
        self.add_task(epoch=2)
        old = self.add_worktree(epoch=1, status="retired")
        new = self.add_worktree(epoch=2, status="active")
        self.assertDeny(claude("Write", str(old / "src/a.py"), str(old)),
                        "worktree %s was retired at epoch 1" % old, "wcp task recover t-aaaaaa")
        self.assertDeny(grok(str(old / "src/a.py"), str(self.other)), "retired")
        # from the new worktree's cwd the builder-escape rule fires first (spec order)
        self.assertDeny(claude("Write", str(old / "src/a.py"), str(new)), "only inside its worktree %s" % new)
        self.assertDeny(codex(["*** Update File: src/a.py"], str(old)), "retired")
        self.assertAllow(claude("Write", str(new / "src/a.py"), str(new)))

    def test_canonical_with_active_task_denies(self):
        self.add_task()
        wt = self.add_worktree()
        self.assertDeny(claude("Write", str(self.root / "src/a.py"), str(self.root)),
                        "canonical checkout is integrator-only", str(wt))
        self.assertDeny(claude("Edit", "src/a.py", str(self.root)), "integrator-only")
        self.assertDeny(grok(str(self.root / "src/a.py"), str(self.root)), "integrator-only")
        self.assertDeny(codex(["*** Update File: src/a.py"], str(self.root)), "integrator-only")
        self.assertDeny(claude("Write", str(self.root / "new/deep/file.py"), str(self.other)), "integrator-only")

    def test_canonical_active_regardless_of_lease_and_state(self):
        states = ("provisioning", "claimed", "blocked", "conflicted", "submitted", "verifying", "publishing")
        for i, state in enumerate(states):
            with self.subTest(state=state):
                self.add_task(tid="t-%06d" % i, state=state, lease=-60.0)
                self.assertDeny(claude("Write", str(self.root / "src/a.py"), str(self.root)), "integrator-only")

    def test_canonical_integrator_role_allows(self):
        self.add_task()
        self.add_worktree()
        p = claude("Write", str(self.root / "src/a.py"), str(self.root))
        self.assertAllow(p, env={"WCP_ROLE": "integrator"})
        self.assertDeny(p, env={"WCP_ROLE": "builder"})
        self.assertDeny(p, env={})

    def test_canonical_no_active_tasks_allows(self):
        for i, state in enumerate(("proposed", "ready", "accepted", "abandoned", "cancelled")):
            self.add_task(tid="t-%06d" % i, state=state)
        self.assertAllow(claude("Write", str(self.root / "src/a.py"), str(self.root)))
        self.assertAllow(grok(str(self.root / "src/a.py"), str(self.root)))

    def test_canonical_reached_through_symlink(self):
        self.add_task()
        link = self.tmp / "link"
        os.symlink(self.root, link)
        self.assertDeny(claude("Write", str(link / "src/a.py"), str(self.other)), "integrator-only")
        self.assertDeny(claude("Write", "src/a.py", str(link)), "integrator-only")

    def test_dotdot_denies_unsafe(self):
        self.add_task()
        wt = self.add_worktree()
        self.assertDeny(claude("Write", str(wt / "src/../src/a.py"), str(wt)), "unsafe path")
        self.assertDeny(claude("Write", "../src/a.py", str(wt / "src")), "unsafe path")
        self.assertDeny(codex(["*** Update File: src/../src/a.py"], str(wt)), "unsafe path")
        self.assertDeny(claude("Write", str(self.root / "src/../src/a.py"), str(self.root)), "unsafe path")

    def test_git_dir_denies_unsafe(self):
        self.add_task()
        wt = self.add_worktree()
        self.assertDeny(claude("Write", str(wt / ".git"), str(wt)), "unsafe path")
        self.assertDeny(claude("Write", str(self.root / ".git/config"), str(self.root)), "unsafe path")
        self.assertDeny(claude("Write", str(wt / "src/a\0.py"), str(wt)), "unsafe path")

    def test_builder_cwd_escape_denies(self):
        self.add_task()
        wt = self.add_worktree()
        needle = "a task builder may write only inside its worktree %s" % wt
        # candidate in canonical
        self.assertDeny(claude("Write", str(self.root / "src/a.py"), str(wt)), needle)
        # candidate in an unregistered directory
        self.assertDeny(claude("Write", str(self.other / "x.py"), str(wt / "src")), needle)
        self.assertDeny(grok(str(self.other / "x.py"), str(wt)), needle)
        # relative path climbing out of the worktree
        self.assertDeny(codex(["*** Update File: ../../escape.py"], str(wt)), "worktree")
        # cwd inside a retired worktree is still a builder cwd
        self.add_task(tid="t-bbbbbb", epoch=2)
        old = self.add_worktree(tid="t-bbbbbb", epoch=1, status="retired")
        self.assertDeny(claude("Write", str(self.other / "x.py"), str(old)), "only inside its worktree")

    def test_write_content_that_looks_like_a_patch_is_not_a_patch(self):
        """Documenting wcp in a file must not be read as a Codex patch, nor echoed in a reason."""
        self.add_task()
        wt = self.add_worktree()
        body = ("how apply_patch works:\n*** Update File: lib/no.py\n*** Add File: /etc/passwd\n"
                "*** Move to: ../escape.py\n")
        allowed = claude("Write", str(wt / "src/doc.md"), str(wt))
        allowed["tool_input"]["content"] = body
        self.assertAllow(allowed)  # the written path is in scope; the body is not a candidate
        denied = claude("Edit", str(wt / "lib/x.py"), str(wt))
        denied["tool_input"].update({"old_string": body, "new_string": body})
        reason = self.assertDeny(denied, "lib/x.py", "scope")
        for needle in ("lib/no.py", "/etc/passwd", "escape.py", "*** ", "apply_patch"):
            self.assertNotIn(needle, reason)
        # the real Codex apply_patch is unaffected
        self.assertDeny(codex(["*** Update File: lib/no.py"], str(wt)), "lib/no.py", "scope")

    def test_first_deny_wins_across_paths(self):
        self.add_task()
        wt = self.add_worktree()
        self.assertDeny(codex(["*** Update File: src/ok.py", "*** Update File: lib/no.py"], str(wt)), "lib/no.py")

    def test_decide_is_read_only(self):
        self.add_task()
        wt = self.add_worktree()
        db_path = db.repo_dir(self.root) / "state.sqlite3"
        before = db_path.read_bytes()
        self.assertAllow(claude("Write", str(wt / "src/a.py"), str(wt)))
        self.assertDeny(claude("Write", str(self.root / "src/a.py"), str(self.root)))
        self.assertEqual(db_path.read_bytes(), before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    def test_decide_fast_path_skips_unrelated_repos_and_historical_worktrees(self):
        # Register an unrelated repo whose DB is corrupted — hook for our repo must never even open it.
        unrelated = self.tmp / "unrelated_repo"
        unrelated.mkdir()
        uconn = db.open_db(unrelated)
        uconn.close()
        (db.repo_dir(unrelated) / "state.sqlite3").write_bytes(b"corrupted unrelated db")
        # Add many historical retired worktrees + one active task in our repo
        for i in range(25):
            tid = "t-%06x" % (i + 100)
            self.add_task(tid=tid, state="accepted", epoch=1)
            self.add_worktree(tid=tid, epoch=1, status="retired")
        self.add_task(tid="t-active", state="running", epoch=1, write=("src/**",))
        wt = self.add_worktree(tid="t-active", epoch=1, status="active")
        self.assertAllow(claude("Write", str(wt / "src/ok.py"), str(wt)))
        self.assertDeny(claude("Write", str(wt / "lib/no.py"), str(wt)), "scope")
        self.assertFalse((self.home / "hook-errors.log").exists())

    def test_sensitive_paths_denied_in_worktree_and_canonical(self):
        self.add_task(write=("**",))
        wt = self.add_worktree()
        for rel in (".env", "src/.env", ".ssh/config", "certs/server.pem", "secrets/k.key"):
            with self.subTest(rel=rel):
                self.assertDeny(claude("Write", str(wt / rel), str(wt)), "sensitive path", rel)
                self.assertDeny(claude("Write", str(self.root / rel), str(self.root)), "sensitive path", rel,
                                env={"WCP_ROLE": "integrator"})
        self.assertAllow(claude("Write", str(wt / ".env.example"), str(wt)))
        self.assertAllow(claude("Write", str(wt / "src/a.py"), str(wt)))
        self.assertAllow(claude("Write", str(self.other / ".env"), str(self.other)))


class MainTests(Base):
    def run_main(self, text, env=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = hooks.main_pretooluse(text, self.home, {} if env is None else env)
        return rc, out.getvalue(), err.getvalue()

    def test_allow_is_silent_zero(self):
        self.add_task()
        wt = self.add_worktree()
        rc, out, err = self.run_main(json.dumps(claude("Write", str(wt / "src/a.py"), str(wt))))
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_deny_is_two_with_stderr_reason(self):
        self.add_task()
        wt = self.add_worktree()
        rc, out, err = self.run_main(json.dumps(claude("Write", str(wt / "lib/x.py"), str(wt))))
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("wcp: lib/x.py is outside task t-aaaaaa scope [src/**]"), err)
        self.assertEqual(err.count("\n"), 1)
        self.assertNotIn(SECRET_MARKER, err)
        rc, out, err = self.run_main(json.dumps(grok(str(self.root / "src/a.py"), str(self.root))))
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("integrator-only", err)
        rc, out, err = self.run_main(json.dumps(claude("Write", str(self.root / "src/a.py"), str(self.root))),
                                     env={"WCP_ROLE": "integrator"})
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_malformed_json_fails_open_and_logs(self):
        rc, out, err = self.run_main("{not json " + SECRET_MARKER)
        self.assertEqual((rc, out, err), (0, "", ""))
        log = self.home / "hook-errors.log"
        text = log.read_text()
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn("JSONDecodeError", text)
        self.assertNotIn(SECRET_MARKER, text)
        self.assertEqual(stat.S_IMODE(os.stat(log).st_mode), 0o600)
        rc, _, _ = self.run_main("[1, 2]")
        self.assertEqual(rc, 0)
        self.assertEqual(len(log.read_text().splitlines()), 2)

    def test_internal_error_fails_open(self):
        self.add_task()
        wt = self.add_worktree()
        self.conn.close()
        db_path = db.repo_dir(self.root) / "state.sqlite3"
        db_path.write_bytes(b"this is not a sqlite database")
        rc, out, err = self.run_main(json.dumps(claude("Write", str(wt / "lib/x.py"), str(wt))))
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertIn("DatabaseError", (self.home / "hook-errors.log").read_text())

    def test_operational_error_fails_closed_in_worktree_and_open_outside(self):
        self.add_task()
        wt = self.add_worktree()
        with mock.patch("wcp.hooks.sqlite3.connect", side_effect=sqlite3.OperationalError("database is locked")):
            # Inside registered worktree: fail closed
            rc, out, err = self.run_main(json.dumps(claude("Write", str(wt / "src/a.py"), str(wt))))
            self.assertEqual((rc, out), (2, ""))
            self.assertTrue(err.startswith("wcp: "), err)
            self.assertIn("unavailable", err)
            # Outside cwd targeting registered worktree: fail closed
            rc, out, err = self.run_main(json.dumps(claude("Write", str(wt / "src/a.py"), str(self.other))))
            self.assertEqual((rc, out), (2, ""))
            # Unregistered directory: fail open so unrelated editor sessions are not blocked
            rc, out, err = self.run_main(json.dumps(claude("Write", str(self.other / "x.py"), str(self.other))))
            self.assertEqual((rc, out, err), (0, "", ""))
        self.assertIn("OperationalError", (self.home / "hook-errors.log").read_text())
        # Real SQLite exclusive lock (shortened connect timeout so the test finishes quickly)
        self.conn.close()
        db_path = db.repo_dir(self.root) / "state.sqlite3"
        locker = sqlite3.connect(str(db_path), isolation_level=None)
        self.addCleanup(locker.close)
        locker.execute("PRAGMA locking_mode=EXCLUSIVE")
        locker.execute("BEGIN EXCLUSIVE")
        real_connect = sqlite3.connect

        def fast_connect(database, **kw):
            kw["timeout"] = 0.05
            return real_connect(database, **kw)

        try:
            with mock.patch("wcp.hooks.sqlite3.connect", side_effect=fast_connect):
                rc, out, err = self.run_main(json.dumps(claude("Write", str(wt / "src/a.py"), str(wt))))
                self.assertEqual((rc, out), (2, ""))
                rc, out, err = self.run_main(json.dumps(claude("Write", str(self.other / "x.py"), str(self.other))))
                self.assertEqual((rc, out, err), (0, "", ""))
        finally:
            locker.execute("ROLLBACK")

    def test_log_written_even_when_home_missing(self):
        shutil.rmtree(self.home)
        rc, _, _ = self.run_main("nope")
        self.assertEqual(rc, 0)
        self.assertTrue((self.home / "hook-errors.log").exists())
        self.assertEqual(stat.S_IMODE(os.stat(self.home).st_mode), 0o700)


class SnippetTests(unittest.TestCase):
    def test_snippets(self):
        for dialect in cli.HOOK_DIALECTS:
            with self.subTest(dialect=dialect):
                snip = hooks.settings_snippet(dialect, "/opt/wcp/bin/wcp")
                entry = snip["hooks"]["PreToolUse"][0]
                hook = entry["hooks"][0]
                self.assertEqual(hook["type"], "command")
                self.assertEqual(hook["command"], "/opt/wcp/bin/wcp hook pretooluse")
                self.assertIsInstance(hook["timeout"], int)
                json.dumps(snip)
        claude_snip = hooks.settings_snippet("claude", "wcp")
        self.assertEqual(claude_snip["hooks"]["PreToolUse"][0]["matcher"], "Edit|Write|NotebookEdit|MultiEdit")
        self.assertEqual(claude_snip["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"], 10)
        self.assertIn("apply_patch", hooks.settings_snippet("codex", "wcp")["hooks"]["PreToolUse"][0]["matcher"])
        qwen = hooks.settings_snippet("qwen", "wcp")["hooks"]["PreToolUse"][0]
        self.assertIn("write_file", qwen["matcher"])
        self.assertGreaterEqual(qwen["hooks"][0]["timeout"], 1000)  # milliseconds
        grok = hooks.settings_snippet("grok", "wcp")["hooks"]["PreToolUse"][0]
        for tool in ("write", "search_replace", "edit_notebook", "delete_file", "Edit", "Write"):
            self.assertIn(tool, grok["matcher"])  # Grok's own ids and the Claude names it aliases
        self.assertEqual(grok["hooks"][0]["timeout"], 10)  # seconds; Grok's own default is 5
        with self.assertRaises(ValueError):
            hooks.settings_snippet("gemini", "wcp")


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wcp-hooks-install-"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_install_merges_without_dropping_keys(self):
        into = self.tmp / ".claude" / "settings.json"
        into.parent.mkdir()
        existing = {
            "model": "opus", "permissions": {"allow": ["Bash(ls)"]},
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "say done"}]}],
                "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "other-guard"}]}],
            },
        }
        into.write_text(json.dumps(existing))
        out = hooks.install_claude(into, "wcp")
        self.assertEqual(out, into)
        data = json.loads(into.read_text())
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["permissions"], {"allow": ["Bash(ls)"]})
        self.assertEqual(data["hooks"]["Stop"], existing["hooks"]["Stop"])
        pre = data["hooks"]["PreToolUse"]
        self.assertEqual(pre[0], existing["hooks"]["PreToolUse"][0])
        self.assertEqual(pre[1], hooks.settings_snippet("claude", "wcp")["hooks"]["PreToolUse"][0])
        hooks.install_claude(into, "wcp")  # idempotent
        self.assertEqual(len(json.loads(into.read_text())["hooks"]["PreToolUse"]), 2)

    def test_install_creates_file(self):
        into = self.tmp / "new" / "settings.json"
        hooks.install_claude(into, "python3 -m wcp")
        data = json.loads(into.read_text())
        self.assertEqual(list(data), ["hooks"])
        self.assertEqual(data["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "python3 -m wcp hook pretooluse")

    def test_install_refuses_non_object(self):
        into = self.tmp / "settings.json"
        into.write_text("[1]")
        with self.assertRaises(ValueError):
            hooks.install_claude(into, "wcp")

    def test_install_merges_every_dialect_idempotently(self):
        """Codex hooks.json, a Grok hooks/<name>.json and a Qwen settings.json take the same shape as
        Claude's: one PreToolUse entry appended, every unrelated key and hook entry left alone."""
        for dialect in cli.HOOK_DIALECTS:
            with self.subTest(dialect=dialect):
                into = self.tmp / dialect / "hooks.json"
                into.parent.mkdir()
                existing = {
                    "model": "keep-me",
                    "hooks": {
                        "PostToolUse": [{"hooks": [{"type": "command", "command": "log-it"}]}],
                        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "other-guard"}]}],
                    },
                }
                into.write_text(json.dumps(existing))
                self.assertEqual(hooks.install(dialect, into, "wcp"), into)
                hooks.install(dialect, into, "wcp")  # idempotent
                data = json.loads(into.read_text())
                self.assertEqual(data["model"], "keep-me")
                self.assertEqual(data["hooks"]["PostToolUse"], existing["hooks"]["PostToolUse"])
                pre = data["hooks"]["PreToolUse"]
                self.assertEqual(pre[0], existing["hooks"]["PreToolUse"][0])
                self.assertEqual(pre[1], hooks.settings_snippet(dialect, "wcp")["hooks"]["PreToolUse"][0])
                self.assertEqual(len(pre), 2)

    def test_install_creates_file_and_parents_for_every_dialect(self):
        for dialect in cli.HOOK_DIALECTS:
            with self.subTest(dialect=dialect):
                into = self.tmp / "fresh" / dialect / "hooks" / "wcp.json"
                hooks.install(dialect, into, "python3 -m wcp")
                data = json.loads(into.read_text())
                self.assertEqual(list(data), ["hooks"])
                entry = data["hooks"]["PreToolUse"][0]
                self.assertEqual(entry["hooks"][0]["command"], "python3 -m wcp hook pretooluse")
                self.assertEqual(entry["matcher"], hooks.settings_snippet(dialect, "wcp")["hooks"]["PreToolUse"][0]["matcher"])


class HookCliTests(unittest.TestCase):
    """`wcp hook print|install --claude|--codex|--grok|--qwen`, against files in a temp directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wcp-hook-cli-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)

    def main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_print_every_dialect(self):
        for dialect in cli.HOOK_DIALECTS:
            with self.subTest(dialect=dialect):
                rc, out, err = self.main(["hook", "print", "--" + dialect])
                self.assertEqual((rc, err), (0, ""))
                self.assertEqual(json.loads(out), hooks.settings_snippet(dialect, cli._wcp_cmd()))

    def test_print_and_install_need_exactly_one_dialect(self):
        for argv in (["hook", "print"], ["hook", "print", "--claude", "--grok"],
                     ["hook", "install", "--into", str(self.tmp / "x.json")],
                     ["hook", "install", "--claude", "--codex", "--into", str(self.tmp / "x.json")]):
            with self.subTest(argv=argv):
                rc, _, _ = self.main(argv)
                self.assertEqual(rc, 3)
        self.assertFalse((self.tmp / "x.json").exists())

    def test_install_every_dialect_writes_only_the_named_file(self):
        for dialect in cli.HOOK_DIALECTS:
            with self.subTest(dialect=dialect):
                into = self.tmp / dialect / "hooks.json"
                rc, out, err = self.main(["hook", "install", "--" + dialect, "--into", str(into), "--json"])
                self.assertEqual((rc, err), (0, ""))
                self.assertEqual(json.loads(out), {"ok": True, "dialect": dialect, "installed": str(into)})
                self.assertEqual(json.loads(into.read_text()),
                                 hooks.settings_snippet(dialect, cli._wcp_cmd()))


if __name__ == "__main__":
    unittest.main()
