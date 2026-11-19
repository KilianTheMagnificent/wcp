from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import unittest
from pathlib import Path
from unittest import mock

from fixtures import write
from test_tasks import Base
from wcp import bundle, cli, db, gitx, integrate, model, tasks
from wcp.db import ProtocolError


def sha(text):
    from wcp import paths
    return paths.sha256_bytes(text.encode())


class GateTests(Base):
    def test_before_hash_drift_leaves_canonical_untouched(self):
        tid, c, s = self.submitted()
        write(self.repo, 'src/a.py', 'VALUE = 9\n')  # canonical moves after the claim
        e = self.refuse('before_hash_drift', integrate.verify, self.conn, self.repo, tid, 'tester')
        self.assertEqual(e.detail['paths'], ['src/a.py'])
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 9\n')
        self.assertEqual(self.canonical('src/b.py'), 'VALUE = 5\n')
        self.assertEqual(self.state(tid), 'conflicted')
        self.assertFalse(os.path.lexists(db.repo_dir(self.repo) / 'integration'))
        self.assertIsNone(self.conn.execute("SELECT holder FROM locks WHERE name='integrator'").fetchone())

    def test_dry_run_leaves_submitted(self):
        tid, c, s = self.submitted()
        v = integrate.verify(self.conn, self.repo, tid, 'tester', dry_run=True)
        self.assertEqual((v['published'], v['state'], v['dry_run']), (False, 'submitted', True))
        self.assertEqual(v['receipts'][0]['exit_code'], 0)
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 5\n')
        self.assertEqual(self.state(tid), 'submitted')
        write(self.repo, 'src/a.py', 'VALUE = 9\n')
        self.refuse('before_hash_drift', integrate.verify, self.conn, self.repo, tid, 'tester', dry_run=True)
        self.assertEqual(self.state(tid), 'submitted')  # dry-run never transitions

    def test_stale_epoch_on_verify_after_release(self):
        tid, c, s = self.submitted()
        tasks.release(self.conn, tid)
        e = self.refuse('stale_epoch', integrate.verify, self.conn, self.repo, tid, 'tester')
        self.assertEqual((e.detail['submission_epoch'], e.detail['epoch']), (1, 2))
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 5\n')

    def test_reject_and_lock_held(self):
        tid, c, s = self.submitted()
        with db.integrator_lock(self.repo, 'someone-else'):
            self.refuse('lock_held', integrate.verify, self.conn, self.repo, tid, 'tester')
            self.refuse('lock_held', integrate.reject, self.conn, tid, 'nope')
        t = integrate.reject(self.conn, tid, 'needs tests')
        self.assertEqual((t.state, t.blocked_reason), ('running', 'rejected: needs tests'))
        self.refuse('bad_state', integrate.verify, self.conn, self.repo, tid, 'tester')
        self.refuse('bad_state', integrate.reject, self.conn, tid, 'again')
        # the owner resubmits and it lands
        tasks.submit(self.conn, self.repo, tid, 'alice', c['epoch'])
        self.assertTrue(integrate.verify(self.conn, self.repo, tid, 'tester')['published'])

    def test_out_of_scope_bundle_path_at_verify(self):
        tid, c, s = self.submitted()
        with db.write_txn(self.conn):
            self.conn.execute("DELETE FROM task_scopes WHERE task_id=?", (tid,))
            self.conn.execute("INSERT INTO task_scopes VALUES (?, 'path', 'docs/**')", (tid,))
        self.refuse('out_of_scope', integrate.verify, self.conn, self.repo, tid, 'tester')
        self.assertEqual(self.state(tid), 'conflicted')
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 5\n')


class InteractionTests(Base):
    def test_fail_together_conflicted(self):
        a, ca, sa = self.submitted('src/a.py', (('src/a.py', 'VALUE = 6\n'),), 'alice')
        b, cb, sb = self.submitted('src/b.py', (('src/b.py', 'VALUE = 6\n'),), 'bob')
        self.assertTrue(integrate.verify(self.conn, self.repo, a, 'tester')['published'])
        e = self.refuse('checks_failed', integrate.verify, self.conn, self.repo, b, 'tester')
        self.assertEqual(e.detail['receipts'][0]['exit_code'], 1)
        self.assertIn('a+b = 12', e.detail['receipts'][0]['output_tail'])
        ev = self.conn.execute("SELECT detail_json FROM events WHERE task_id=? AND kind='verify_failed'", (b,)).fetchone()[0]
        self.assertIn('"exit_code": 1', ev)
        self.assertNotIn('a+b = 12', ev)  # the event carries the receipt minus its output: `wcp events` is not a read-back
        self.assertEqual(self.state(b), 'conflicted')
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 6\n')
        self.assertEqual(self.canonical('src/b.py'), 'VALUE = 5\n')
        self.assertEqual(self.state(a), 'accepted')
        # bob's worktree is intact; he resumes, fixes, resubmits
        t = tasks.heartbeat(self.conn, b, 'bob', cb['epoch'])
        self.assertEqual(t.state, 'running')
        self.assertEqual((Path(cb['worktree']) / 'src/b.py').read_text(), 'VALUE = 6\n')

    def test_checks_failed_without_interaction_returns_to_running(self):
        tid, c, s = self.submitted('src/a.py', (('src/a.py', 'VALUE = 7\n'),))  # 7+5 = 12
        self.refuse('checks_failed', integrate.verify, self.conn, self.repo, tid, 'tester')
        self.assertEqual(self.state(tid), 'running')
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 5\n')

    def test_candidate_mutated_when_check_rewrites_bundle_path(self):
        rc, out = self.cli('init', '--check', 'python3 tests/check.py', '--check', "printf 'VALUE = 9\\n' > src/a.py")
        self.assertEqual(rc, 0)
        tid, c, s = self.submitted()
        e = self.refuse('candidate_mutated', integrate.verify, self.conn, self.repo, tid, 'tester')
        self.assertEqual(e.detail['paths'], ['src/a.py'])
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 5\n')
        self.assertEqual(self.state(tid), 'running')
        self.assertFalse(os.path.lexists(db.repo_dir(self.repo) / 'integration'))

    def test_verify_runs_only_init_declared_checks(self):
        """A task-level --check is declared by whoever ran task create; the integrator never executes it."""
        tid = self.task('src/a.py', checks=('echo TASK-CHECK-RAN',))
        c = self.claim(tid)
        write(c['worktree'], 'src/a.py', 'VALUE = 6\n')
        s = tasks.submit(self.conn, self.repo, tid, 'alice', c['epoch'])
        self.assertEqual([r['cmd'] for r in s['receipts']], ['echo TASK-CHECK-RAN'])  # submit phase, builder's process
        ran = []
        real = gitx.run_check

        def record(check, cwd):
            ran.append(check.get('cmd') or check.get('argv'))
            return real(check, cwd)

        with mock.patch.object(gitx, 'run_check', record):
            v = integrate.verify(self.conn, self.repo, tid, 'tester')
        self.assertTrue(v['published'])
        self.assertEqual(ran, ['python3 tests/check.py'])
        self.assertEqual([r['cmd'] for r in v['receipts']], ['python3 tests/check.py'])
        rows = self.conn.execute("SELECT phase, cmd FROM receipts WHERE task_id=? ORDER BY id", (tid,)).fetchall()
        self.assertEqual([tuple(r) for r in rows], [('submit', 'echo TASK-CHECK-RAN'), ('verify', 'python3 tests/check.py')])

    def test_candidate_mutated_when_check_rewrites_non_bundle_path(self):
        self.cli('init', '--check', "printf 'x' > README.md")
        tid, c, s = self.submitted()
        e = self.refuse('candidate_mutated', integrate.verify, self.conn, self.repo, tid, 'tester')
        self.assertEqual(e.detail['paths'], ['README.md'])
        self.assertEqual(self.canonical('README.md'), '# fixture\n')


class PublishTests(Base):
    def test_publish_cas_refuses_on_version_drift(self):
        tid, c, s = self.submitted()
        real = gitx.run_check

        def drift_then_run(check, cwd):
            other = db.open_db(self.repo)
            try:
                with db.write_txn(other):
                    model.transition(other, model.load_task(other, tid), 'running')  # e.g. a concurrent reject
            finally:
                other.close()
            return real(check, cwd)

        with mock.patch.object(gitx, 'run_check', drift_then_run):
            e = self.refuse('stale_version', integrate.verify, self.conn, self.repo, tid, 'tester')
        self.assertEqual(e.detail['state'], 'running')
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 5\n')
        self.assertEqual(self.state(tid), 'running')
        kinds = [r['kind'] for r in self.conn.execute('SELECT kind FROM events WHERE task_id=?', (tid,))]
        self.assertNotIn('publish_started', kinds)

    def test_publish_drift_during_checks_conflicts(self):
        tid, c, s = self.submitted()
        real = gitx.run_check

        def move_canonical(check, cwd):
            write(self.repo, 'src/a.py', 'VALUE = 9\n')
            return real(check, cwd)

        with mock.patch.object(gitx, 'run_check', move_canonical):
            self.refuse('before_hash_drift', integrate.verify, self.conn, self.repo, tid, 'tester')
        self.assertEqual(self.state(tid), 'conflicted')
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 9\n')

    def _publishing(self):
        """A real two-path submission hand-set to 'publishing' as if the integrator died mid-apply."""
        tid, c, s = self.submitted('src/**', (('src/a.py', 'VALUE = 6\n'), ('src/b.py', 'VALUE = 4\n')))
        with db.write_txn(self.conn):
            self.conn.execute("UPDATE tasks SET state='publishing', version=version+1 WHERE id=?", (tid,))
        return tid, s

    def test_publish_recovery_completes(self):
        tid, s = self._publishing()
        write(self.repo, 'src/a.py', 'VALUE = 6\n')  # already at after; src/b.py still at before
        v = integrate.verify(self.conn, self.repo, tid, 'tester')
        self.assertEqual((v['state'], v['published'], v['paths'], v['recovered']), ('accepted', True, ['src/b.py'], True))
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 6\n')
        self.assertEqual(self.canonical('src/b.py'), 'VALUE = 4\n')
        self.assertEqual(self.state(tid), 'accepted')
        self.refuse('bad_state', integrate.verify, self.conn, self.repo, tid, 'tester')

    def test_dry_run_on_publishing_writes_nothing(self):
        tid, s = self._publishing()
        v = integrate.verify(self.conn, self.repo, tid, 'tester', dry_run=True)
        self.assertEqual((v['state'], v['published'], v['dry_run'], v['paths']), ('publishing', False, True, []))
        self.assertEqual(v['classes'], {'src/a.py': 'before', 'src/b.py': 'before'})
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 5\n')  # byte-identical: nothing applied
        self.assertEqual(self.canonical('src/b.py'), 'VALUE = 5\n')
        self.assertEqual(self.state(tid), 'publishing')
        write(self.repo, 'src/a.py', 'VALUE = 99\n')  # a third state is reported, not quarantined, under dry-run
        v = integrate.verify(self.conn, self.repo, tid, 'tester', dry_run=True)
        self.assertEqual(v['classes'], {'src/a.py': 'other', 'src/b.py': 'before'})
        self.assertEqual(self.state(tid), 'publishing')
        self.assertEqual(self.canonical('src/b.py'), 'VALUE = 5\n')
        kinds = [r['kind'] for r in self.conn.execute('SELECT kind FROM events WHERE task_id=?', (tid,))]
        self.assertNotIn('publish_finished', kinds)
        self.assertNotIn('partial_publish', kinds)
        write(self.repo, 'src/a.py', 'VALUE = 6\n')
        self.assertTrue(integrate.verify(self.conn, self.repo, tid, 'tester')['published'])  # the real run completes
        self.assertEqual(self.canonical('src/b.py'), 'VALUE = 4\n')

    def test_publish_recovery_quarantines_third_state(self):
        tid, s = self._publishing()
        write(self.repo, 'src/a.py', 'VALUE = 99\n')  # neither before nor after
        e = self.refuse('partial_publish', integrate.verify, self.conn, self.repo, tid, 'tester')
        self.assertEqual(e.detail['paths'], ['src/a.py'])
        self.assertEqual(e.detail['classes'], {'src/a.py': 'other', 'src/b.py': 'before'})
        self.assertEqual(self.state(tid), 'conflicted')
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 99\n')
        self.assertEqual(self.canonical('src/b.py'), 'VALUE = 5\n')  # nothing further was written

    def test_exec_bit_and_nested_create_publish(self):
        tid = self.task('src/**', 'tools/**')
        c = self.claim(tid)
        wt = Path(c['worktree'])
        os.chmod(wt / 'src/b.py', 0o755)
        write(wt, 'tools/new/run.sh', '#!/bin/sh\n')
        os.chmod(wt / 'tools/new/run.sh', 0o755)
        s = tasks.submit(self.conn, self.repo, tid, 'alice', c['epoch'])
        self.assertEqual({ch['path']: ch['op'] for ch in s['changes']}, {'src/b.py': 'chmod', 'tools/new/run.sh': 'create'})
        v = integrate.verify(self.conn, self.repo, tid, 'tester')
        self.assertEqual(v['paths'], ['src/b.py', 'tools/new/run.sh'])
        self.assertTrue(os.stat(self.repo / 'src/b.py').st_mode & 0o111)
        self.assertTrue(os.stat(self.repo / 'tools/new/run.sh').st_mode & 0o111)
        self.assertEqual(self.canonical('src/b.py'), 'VALUE = 5\n')

    def test_publish_preserves_existing_permission_bits(self):
        os.chmod(self.repo / 'src/a.py', 0o600)  # a private canonical file, git tracks only the exec bit
        os.chmod(self.repo / 'src/b.py', 0o600)
        tid = self.task('src/**')
        c = self.claim(tid)
        wt = Path(c['worktree'])
        write(wt, 'src/a.py', 'VALUE = 6\n')
        write(wt, 'src/b.py', 'VALUE = 5  # exec\n')
        os.chmod(wt / 'src/b.py', 0o755)
        s = tasks.submit(self.conn, self.repo, tid, 'alice', c['epoch'])
        self.assertEqual({ch['path']: ch['after_mode'] for ch in s['changes']},
                         {'src/a.py': '100644', 'src/b.py': '100755'})
        self.assertTrue(integrate.verify(self.conn, self.repo, tid, 'tester')['published'])
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 6\n')
        self.assertEqual(stat.S_IMODE(os.stat(self.repo / 'src/a.py').st_mode), 0o600)  # not widened to 0644
        self.assertEqual(stat.S_IMODE(os.stat(self.repo / 'src/b.py').st_mode), 0o700)  # exec follows the manifest

    def test_accept_alias_and_cli_receipts(self):
        tid, c, s = self.submitted()
        rc, out = self.cli('task', 'accept', tid)
        self.assertEqual(rc, 0, out)
        self.assertEqual((out['state'], out['published']), ('accepted', True))
        self.assertEqual(out['receipts'][0]['trust'], 'untrusted_observation')
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 6\n')
        rc, out = self.cli('task', 'verify', tid)
        self.assertEqual((rc, out['code']), (3, 'bad_state'))


class WaitTests(Base):
    """Addendum A3: verify --wait polls db.sleep(2) around lock_held only."""
    _CHILD = ("import sys\nfrom pathlib import Path\nfrom wcp import db\n"
              "with db.integrator_lock(Path(sys.argv[1]), 'child'):\n"
              "    sys.stdout.write('held\\n'); sys.stdout.flush()\n"
              "    sys.stdin.readline()\n"
              "sys.stdout.write('released\\n'); sys.stdout.flush()\n")

    def _child_holding_lock(self):
        import subprocess
        import sys
        project = Path(__file__).resolve().parent.parent
        child = subprocess.Popen([sys.executable, '-c', self._CHILD, str(self.repo)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, env=dict(os.environ, PYTHONPATH=str(project)),
                                 cwd=str(project), text=True)
        self.addCleanup(lambda: (child.poll() is None and child.kill(), child.stdin.close(), child.stdout.close()))
        self.assertEqual(child.stdout.readline().strip(), 'held')
        return child

    def test_verify_wait_acquires_after_child_flock_releases(self):
        tid, c, s = self.submitted()
        child = self._child_holding_lock()
        sleeps = []
        with mock.patch.object(db, 'sleep', sleeps.append):
            e = self.refuse('lock_held', integrate.verify, self.conn, self.repo, tid, 'tester')  # no --wait
            self.assertEqual((e.detail['holder'], sleeps), ('child', []))

        def sleep(seconds):
            sleeps.append(seconds)
            child.stdin.write('\n')
            child.stdin.flush()
            self.assertEqual(child.stdout.readline().strip(), 'released')
            self.assertEqual(child.wait(timeout=30), 0)

        with mock.patch.object(db, 'sleep', sleep):
            v = integrate.verify(self.conn, self.repo, tid, 'tester', wait_s=-1)
        self.assertEqual((v['published'], v['state'], sleeps), (True, 'accepted', [2]))
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 6\n')

    def test_verify_wait_timeout_and_other_refusals_never_wait(self):
        tid, c, s = self.submitted()
        child = self._child_holding_lock()
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            self.offset += seconds

        with mock.patch.object(db, 'sleep', sleep):
            e = self.refuse('waiting_timeout', integrate.verify, self.conn, self.repo, tid, 'tester', wait_s=3)
        self.assertEqual((e.detail['holder'], e.detail['task_id'], sleeps), ('child', tid, [2, 2]))
        self.assertEqual(self.state(tid), 'submitted')  # nothing was reserved
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 5\n')
        child.stdin.write('\n')
        child.stdin.flush()
        self.assertEqual(child.wait(timeout=30), 0)
        tasks.release(self.conn, tid)  # a stale epoch is not a thing to wait for
        with mock.patch.object(db, 'sleep', lambda s_: self.fail('slept on stale_epoch')):
            self.refuse('stale_epoch', integrate.verify, self.conn, self.repo, tid, 'tester', wait_s=-1)
        rc, out = self.cli('task', 'verify', tid, '--wait', '0')
        self.assertEqual((rc, out['code']), (3, 'stale_epoch'))
        rc, out = self.cli('task', 'accept', tid, '--wait', '0')
        self.assertEqual((rc, out['code']), (3, 'stale_epoch'))


class ReceiptTests(Base):
    def test_verify_check_output_is_returned_but_never_stored(self):
        """A verify check runs with the integrator's read access to canonical; its output must not
        become durable state that `task show`/`task recover` hands back to a sandboxed agent."""
        verify_canary, submit_canary = 'CANARY-FROM-CANONICAL', 'CANARY-FROM-BUILDER'
        secret = self.home / 'reachable-only-by-the-integrator.txt'  # stands in for canonical content
        secret.write_text(verify_canary + '\n')
        rc, out = self.cli('init', '--check', 'cat %s' % secret)  # the canary is in the output, not the cmd
        self.assertEqual(rc, 0, out)
        tid = self.task('src/a.py', checks=('echo %s' % submit_canary,))
        c = self.claim(tid)
        write(c['worktree'], 'src/a.py', 'VALUE = 6\n')
        s = tasks.submit(self.conn, self.repo, tid, 'alice', c['epoch'])
        self.assertIn(submit_canary, s['receipts'][0]['output_tail'])
        rc, v = self.cli('task', 'accept', tid)  # the invocation that ran the check does see the tail
        self.assertEqual(rc, 0, v)
        self.assertIn(verify_canary, v['receipts'][0]['output_tail'])
        rows = self.conn.execute('SELECT phase, exit_code, output_tail FROM receipts WHERE task_id=? ORDER BY id',
                                 (tid,)).fetchall()
        self.assertEqual([r['phase'] for r in rows], ['submit', 'verify'])
        self.assertIn(submit_canary, rows[0]['output_tail'])  # the builder's own process: unchanged
        self.assertEqual(rows[1]['exit_code'], 0)             # cmd/exit/duration still recorded
        self.assertFalse(rows[1]['output_tail'])              # the tail is not
        for args in (('task', 'show', tid), ('task', 'recover', tid)):
            rc, served = self.cli(*args)
            self.assertEqual(rc, 0, served)
            text = json.dumps(served)
            self.assertIn(submit_canary, text)                # receipts are served...
            self.assertNotIn(verify_canary, text)             # ...but never the verify output


if __name__ == '__main__':
    unittest.main()
