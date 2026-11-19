from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import time
import unittest
from pathlib import Path
from unittest import mock

from fixtures import make_home, make_repo, write
from wcp import cli, db, gitx, integrate, model, tasks
from wcp.db import ProtocolError

_REAL_NOW = db.now


class Base(unittest.TestCase):
    """Temp WCP_HOME + fixture repo registered with the a+b <= 11 check; db.now() is offsettable."""

    def setUp(self):
        self.home = make_home()
        self.addCleanup(shutil.rmtree, str(self.home), True)
        self.repo = make_repo()
        self.addCleanup(shutil.rmtree, str(self.repo), True)
        self.offset = 0.0
        db.now = lambda: _REAL_NOW() + self.offset
        self.addCleanup(setattr, db, 'now', _REAL_NOW)
        rc, out = self.cli('init', '--check', 'python3 tests/check.py')
        self.assertEqual(rc, 0, out)
        self.conn = db.open_db(self.repo)
        self.addCleanup(self.conn.close)

    def cli(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(['--json', '--repo', str(self.repo)] + [str(a) for a in args])
        text = buf.getvalue().strip()
        return rc, (json.loads(text) if text else {})

    def task(self, *write, resource=(), goal='task', checks=(), depends=(), ready=True):
        t = tasks.create_task(self.conn, goal, write, resource, depends, checks)
        if ready:
            tasks.ready_task(self.conn, t.id)
        return t.id

    def claim(self, tid, owner='alice', lease_s=900):
        return tasks.claim_task(self.conn, self.repo, tid, owner, lease_s)

    def expire(self):
        self.offset += 100000.0

    def refuse(self, code, fn, *args, **kw):
        with self.assertRaises(ProtocolError) as cm:
            fn(*args, **kw)
        self.assertEqual(cm.exception.code, code, cm.exception.message)
        return cm.exception

    def state(self, tid):
        return model.load_task(self.conn, tid).state

    def canonical(self, rel):
        return (self.repo / rel).read_text()

    def submitted(self, scope='src/a.py', edits=(('src/a.py', 'VALUE = 6\n'),), owner='alice'):
        """create -> ready -> claim -> heartbeat -> edit -> submit; returns (tid, claim dict, submit dict)."""
        tid = self.task(scope)
        c = self.claim(tid, owner)
        tasks.heartbeat(self.conn, tid, owner, c['epoch'])
        for rel, text in edits:
            write(c['worktree'], rel, text)
        s = tasks.submit(self.conn, self.repo, tid, owner, c['epoch'])
        return tid, c, s


class LifecycleTests(Base):
    def test_happy_path_then_second_task_can_claim(self):
        tid, c, s = self.submitted()
        self.assertEqual(s['state'], 'submitted')
        self.assertEqual([ch['path'] for ch in s['changes']], ['src/a.py'])
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 5\n')
        v = integrate.verify(self.conn, self.repo, tid, 'tester')
        self.assertTrue(v['published'])
        self.assertEqual(v['state'], 'accepted')
        self.assertEqual(v['paths'], ['src/a.py'])
        self.assertEqual(v['receipts'][0]['trust'], 'untrusted_observation')
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 6\n')
        self.assertEqual(self.canonical('src/b.py'), 'VALUE = 5\n')
        kinds = [r['kind'] for r in self.conn.execute('SELECT kind FROM events WHERE task_id=? ORDER BY id', (tid,))]
        self.assertIn('publish_started', kinds)
        self.assertIn('publish_finished', kinds)
        # scope released: an overlapping task is admitted and claimed, and sees the published edit
        t2 = self.task('src/a.py')
        c2 = self.claim(t2, 'bob')
        self.assertEqual(c2['epoch'], 2)
        self.assertEqual((Path(c2['worktree']) / 'src/a.py').read_text(), 'VALUE = 6\n')
        self.assertEqual(self.state(t2), 'claimed')

    def test_claim_output_and_hint(self):
        tid = self.task('src/a.py')
        c = self.claim(tid)
        self.assertTrue(c['worktree'].startswith(str(self.home)))
        self.assertIn('-e1', c['worktree'])
        self.assertEqual(c['codex_hint'], 'codex -C %s -s workspace-write' % c['worktree'])
        self.assertIn("orch:", c['owner_note'])  # Orchestrated worker owners are readable in `wcp ps`
        self.assertEqual(c['recovered'], [])
        self.assertEqual(c['rebase_needed'], [])
        self.assertEqual(gitx.registration(self.repo, Path(c['worktree'])), 'active')
        rows = model.load_worktrees(self.conn, tid)
        self.assertEqual([(w.status, w.epoch) for w in rows], [('active', 1)])
        self.assertTrue(tasks.load_baseline(self.conn, c['worktree']))

    def test_second_claim_refused_and_one_epoch_consumed(self):
        tid = self.task('src/a.py')
        self.claim(tid, 'alice')
        self.refuse('bad_state', self.claim, tid, 'bob')
        self.assertEqual(db.meta_get(self.conn, 'epoch'), '1')
        self.assertEqual(model.load_task(self.conn, tid).owner, 'alice')

    def test_stale_epoch_after_release(self):
        tid = self.task('src/a.py')
        c = self.claim(tid)
        rel = tasks.release(self.conn, tid)
        self.assertEqual(rel.epoch, 2)
        self.assertTrue(rel.lease_expired(db.now()))
        self.refuse('stale_epoch', tasks.heartbeat, self.conn, tid, 'alice', c['epoch'])
        self.refuse('stale_epoch', tasks.submit, self.conn, self.repo, tid, 'alice', c['epoch'])
        # the same owner reclaims under the new epoch; old worktree retired, fresh one provisioned
        c2 = self.claim(tid, 'alice')
        self.assertEqual(c2['epoch'], 3)
        self.assertNotEqual(c2['worktree'], c['worktree'])
        self.assertEqual({w.path: w.status for w in model.load_worktrees(self.conn, tid)},
                         {c['worktree']: 'retired', c2['worktree']: 'active'})

    def test_out_of_scope_on_submit_stores_nothing(self):
        tid = self.task('src/a.py')
        c = self.claim(tid)
        write(c['worktree'], 'src/b.py', 'VALUE = 6\n')
        e = self.refuse('out_of_scope', tasks.submit, self.conn, self.repo, tid, 'alice', c['epoch'])
        self.assertEqual(e.detail['paths'], ['src/b.py'])
        self.assertIsNone(tasks.latest_submission(self.conn, tid))
        self.assertEqual(self.state(tid), 'claimed')
        self.assertEqual(list((self.home / 'repos').glob('*/bundles/*')), [])

    def test_submit_checks_failed_stays_running(self):
        tid = self.task('src/a.py', checks=('python3 tests/check.py',))
        c = self.claim(tid)
        write(c['worktree'], 'src/a.py', 'VALUE = 7\n')  # 12 > 11
        e = self.refuse('checks_failed', tasks.submit, self.conn, self.repo, tid, 'alice', c['epoch'])
        self.assertEqual(self.state(tid), 'running')
        self.assertIsNone(tasks.latest_submission(self.conn, tid))
        self.assertEqual(e.detail['receipts'][0]['exit_code'], 1)
        self.refuse('checks_failed', tasks.submit, self.conn, self.repo, tid, 'alice', c['epoch'], record_failing=True)
        self.assertIsNotNone(tasks.latest_submission(self.conn, tid))
        self.assertEqual(self.state(tid), 'running')
        self.refuse('bad_state', integrate.verify, self.conn, self.repo, tid, 'tester')

    def test_submit_survives_owner_heartbeat_during_checks(self):
        tid = self.task('src/a.py', checks=('python3 tests/check.py',))
        c = self.claim(tid)
        write(c['worktree'], 'src/a.py', 'VALUE = 6\n')
        real = gitx.run_check
        beats = []

        def heartbeat_then_run(check, cwd):  # the builder's heartbeat loop fires while the check runs
            other = db.open_db(self.repo)
            try:
                beats.append(tasks.heartbeat(other, tid, 'alice', c['epoch']).version)
            finally:
                other.close()
            return real(check, cwd)

        with mock.patch.object(gitx, 'run_check', heartbeat_then_run):
            s = tasks.submit(self.conn, self.repo, tid, 'alice', c['epoch'])
        self.assertEqual(len(beats), 1)
        self.assertEqual(s['state'], 'submitted')
        self.assertEqual(self.state(tid), 'submitted')
        self.assertIsNotNone(tasks.latest_submission(self.conn, tid))
        self.assertEqual(s['receipts'][0]['exit_code'], 0)
        # a stale explicit --expect-version is still refused
        c2 = self.claim(self.task('src/b.py'), 'bob')
        write(c2['worktree'], 'src/b.py', 'VALUE = 6\n')
        self.refuse('stale_version', tasks.submit, self.conn, self.repo, c2['task'].id, 'bob', c2['epoch'],
                    expect_version=c2['task'].version + 5)

    def test_owner_gates(self):
        tid = self.task('src/a.py')
        c = self.claim(tid)
        self.refuse('not_owner', tasks.heartbeat, self.conn, tid, 'mallory', c['epoch'])
        self.refuse('stale_epoch', tasks.heartbeat, self.conn, tid, 'alice', c['epoch'] + 1)
        self.expire()
        self.refuse('lease_expired', tasks.heartbeat, self.conn, tid, 'alice', c['epoch'])

    def test_block_abandon_cancel(self):
        tid = self.task('src/a.py')
        c = self.claim(tid)
        t = tasks.block(self.conn, tid, 'alice', c['epoch'], 'waiting on scope')
        self.assertEqual((t.state, t.blocked_reason), ('blocked', 'waiting on scope'))
        t = tasks.heartbeat(self.conn, tid, 'alice', c['epoch'])
        self.assertEqual(t.state, 'running')
        self.refuse('stale_version', tasks.abandon, self.conn, tid, 'alice', c['epoch'], expect_version=1)
        t = tasks.abandon(self.conn, tid, 'alice', c['epoch'], expect_version=t.version)
        self.assertEqual(t.state, 'abandoned')
        t2 = self.task('src/a.py')  # scope released by abandon
        self.assertEqual(tasks.cancel(self.conn, t2).state, 'cancelled')
        self.refuse('bad_state', tasks.cancel, self.conn, t2)

    def test_cancel_and_release_refused_on_verifying(self):
        tid, c, s = self.submitted()
        with db.write_txn(self.conn):
            model.transition(self.conn, model.load_task(self.conn, tid), 'verifying')
        self.refuse('bad_state', tasks.cancel, self.conn, tid)
        self.refuse('bad_state', tasks.release, self.conn, tid)
        self.refuse('bad_state', self.claim, tid, 'bob')
        self.expire()
        self.refuse('bad_state', self.claim, tid, 'bob')  # never claimable, even with an expired lease
        self.assertEqual(self.state(tid), 'verifying')

    def test_deps(self):
        a = self.task('src/a.py')
        b = self.task('src/b.py', depends=(a,))
        self.refuse('deps_unmet', self.claim, b, 'bob')
        self.refuse('not_found', tasks.ready_task, self.conn, self.task('docs/**', depends=('t-nope',), ready=False))
        c = self.claim(a)
        write(c['worktree'], 'src/a.py', 'VALUE = 6\n')
        tasks.submit(self.conn, self.repo, a, 'alice', c['epoch'])
        integrate.verify(self.conn, self.repo, a, 'tester')
        self.assertEqual(self.claim(b, 'bob')['task'].state, 'claimed')


class OverlapTests(Base):
    def test_overlap_reserved_while_lease_expired(self):
        """An expired lease keeps the scope reserved: `ready` reports it (A1), `claim` still refuses it."""
        a = self.task('src/a.py')
        self.claim(a)
        self.expire()
        b = tasks.create_task(self.conn, 'b', ('src/**',))
        self.assertEqual(tasks.ready_task(self.conn, b.id).state, 'ready')
        self.assertEqual(tasks.waiting_on(self.conn, b.id, db.now()), [a])
        e = self.refuse('scope_overlap', self.claim, b.id, 'bob')
        self.assertEqual((e.detail['other'], e.detail['waiting_on']), (a, [a]))
        self.assertEqual(self.state(b.id), 'ready')

    def test_ready_ready_overlap_and_case_alias(self):
        """Two ready tasks may overlap (A1); the atomic claim is where they serialize, NFC/casefold included."""
        a = self.task('src/a.py')
        b = self.task('SRC/A.PY')
        self.claim(a, 'alice')
        self.refuse('scope_overlap', self.claim, b, 'bob')
        r = self.task(resource=('port:5199',))
        r2 = self.task('docs/**', resource=('port:5199',))
        self.claim(r, 'ray')
        e = self.refuse('scope_overlap', self.claim, r2, 'rita')
        self.assertEqual((e.detail['resources'], e.detail['waiting_on']), (['port:5199'], [r]))

    def test_scope_request_approve_is_overlap_checked(self):
        a = self.task('src/a.py')
        b = self.task('src/b.py')
        self.claim(a, 'alice')
        cb = self.claim(b, 'bob')
        req = tasks.request_scope(self.conn, b, 'bob', cb['epoch'], 'path', 'src/a.py', 'need it')
        self.refuse('scope_overlap', tasks.approve_scope, self.conn, b, req)
        req2 = tasks.request_scope(self.conn, b, 'bob', cb['epoch'], 'path', 'docs/**', 'docs too')
        self.assertEqual(tasks.approve_scope(self.conn, b, req2).status, 'approved')
        self.assertEqual(model.load_scopes(self.conn, b)['path'], ['docs/**', 'src/b.py'])
        self.assertEqual(tasks.deny_scope(self.conn, b, req, 'no').status, 'denied')
        self.refuse('bad_state', tasks.approve_scope, self.conn, b, req)
        self.refuse('sensitive_path', tasks.request_scope, self.conn, b, 'bob', cb['epoch'], 'path', '.env', 'x')

    def test_scope_approve_refused_once_integrator_owned_or_terminal(self):
        tid, c, s = self.submitted()
        req = tasks.request_scope(self.conn, tid, 'alice', c['epoch'], 'path', 'docs/**', 'later')
        for state in ('verifying', 'publishing', 'accepted'):
            with db.write_txn(self.conn):
                model.transition(self.conn, model.load_task(self.conn, tid), state)
            with self.subTest(state=state):
                self.refuse('bad_state', tasks.approve_scope, self.conn, tid, req)
        self.assertEqual(model.load_scopes(self.conn, tid)['path'], ['src/a.py'])
        self.assertEqual(self.conn.execute('SELECT status FROM scope_requests WHERE id=?',
                                           (req,)).fetchone()['status'], 'pending')

    def test_create_refusals(self):
        self.refuse('sensitive_path', tasks.create_task, self.conn, 'x', ('config/.env',))
        self.refuse('unsafe_path', tasks.create_task, self.conn, 'x', ('../etc/**',))
        with db.write_txn(self.conn):
            db.meta_set(self.conn, 'preserve_roots', json.dumps(['vendor']))
        self.refuse('preserve_root', tasks.create_task, self.conn, 'x', ('vendor/lib.py',))
        self.refuse('preserve_root', tasks.create_task, self.conn, 'x', ('**',))
        tasks.create_task(self.conn, 'x', ('src/**',))


class ReclaimTests(Base):
    def test_reclaim_transfers_unchanged_baseline_edit(self):
        tid = self.task('src/a.py')
        c1 = self.claim(tid, 'alice')
        write(c1['worktree'], 'src/a.py', 'VALUE = 6\n')
        self.expire()
        c2 = self.claim(tid, 'bob')
        self.assertEqual(c2['epoch'], 2)
        self.assertNotEqual(c2['worktree'], c1['worktree'])
        self.assertEqual((Path(c2['worktree']) / 'src/a.py').read_text(), 'VALUE = 6\n')
        self.assertEqual([r['path'] for r in c2['recovered']], ['src/a.py'])
        self.assertEqual(c2['rebase_needed'], [])
        self.assertTrue(Path(c1['worktree']).is_dir())
        self.assertEqual({w.path: w.status for w in model.load_worktrees(self.conn, tid)},
                         {c1['worktree']: 'retired', c2['worktree']: 'active'})
        self.refuse('stale_epoch', tasks.heartbeat, self.conn, tid, 'alice', c1['epoch'])
        # the new owner submits + verifies under the new epoch; the transferred edit is what lands
        s = tasks.submit(self.conn, self.repo, tid, 'bob', c2['epoch'])
        self.assertEqual([ch['path'] for ch in s['changes']], ['src/a.py'])
        integrate.verify(self.conn, self.repo, tid, 'tester')
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 6\n')

    def test_reclaim_skips_moved_baseline(self):
        tid = self.task('src/a.py')
        c1 = self.claim(tid, 'alice')
        write(c1['worktree'], 'src/a.py', 'VALUE = 6\n')
        write(self.repo, 'src/a.py', 'VALUE = 7\n')  # canonical moves underneath (uncommitted)
        self.expire()
        c2 = self.claim(tid, 'bob')
        self.assertEqual(c2['recovered'], [])
        self.assertEqual([r['path'] for r in c2['rebase_needed']], ['src/a.py'])
        self.assertEqual(c2['rebase_needed'][0]['retired_worktree'], c1['worktree'])
        self.assertEqual((Path(c2['worktree']) / 'src/a.py').read_text(), 'VALUE = 7\n')
        self.assertEqual((Path(c1['worktree']) / 'src/a.py').read_text(), 'VALUE = 6\n')
        rec = __import__('wcp.recover', fromlist=['recover']).recover(self.conn, self.repo, tid, db.now())
        self.assertEqual([r['path'] for r in rec['rebase_needed']], ['src/a.py'])
        self.assertTrue(any('rebase_needed' in b for b in rec['blockers']))
        # a stranger's reclaim records the obligation too: bob cannot publish over the moved path by omission
        self.assertEqual([(o.path, o.status, o.retired_worktree) for o in model.load_obligations(self.conn, tid, 2)],
                         [('src/a.py', 'pending', c1['worktree'])])
        e = self.refuse('rebase_pending', tasks.submit, self.conn, self.repo, tid, 'bob', c2['epoch'])
        self.assertEqual((e.detail['paths'], e.detail['retired_worktree']), (['src/a.py'], c1['worktree']))
        tasks.ack_obligation(self.conn, tid, 'bob', c2['epoch'], 'src/a.py')
        self.refuse('bad_state', tasks.submit, self.conn, self.repo, tid, 'bob', c2['epoch'])  # nothing changed

    def test_provisioning_failure_keeps_a_row_it_could_not_remove(self):
        tid = self.task('src/a.py')
        scratch = db.repo_dir(self.repo) / 'worktrees' / ('%s-e1' % tid)

        def crash(root, dest, links):  # leaves the directory on disk and unregistered with git
            write(dest, 'notes.txt', 'half-provisioned\n')
            raise gitx.GitError('provisioning blew up')

        with mock.patch.object(gitx, 'provision_worktree', crash):
            with self.assertRaises(gitx.GitError):
                self.claim(tid, 'alice')
        self.assertFalse(gitx.remove_worktree(self.repo, scratch, force=True))
        self.assertTrue(scratch.is_dir())
        self.assertEqual(self.state(tid), 'ready')  # the task is claimable again
        self.assertEqual({w.path: w.status for w in model.load_worktrees(self.conn, tid)},
                         {str(scratch): 'provisioning'})  # the path stays attributable

    def test_provisioning_crash_is_reprovisioned(self):
        tid = self.task('src/a.py')
        scratch = db.repo_dir(self.repo) / 'worktrees' / ('%s-e1' % tid)
        with db.write_txn(self.conn):
            epoch = db.next_epoch(self.conn)
            model.transition(self.conn, model.load_task(self.conn, tid), 'provisioning', owner='ghost', epoch=epoch,
                             lease_expires_at=db.now() - 10, worktree_path=str(scratch))
            self.conn.execute("INSERT INTO worktrees(path, task_id, epoch, status, created_at) VALUES (?,?,?,'provisioning',?)",
                              (str(scratch), tid, epoch, db.now()))
        db.ensure_private(scratch.parent, True)
        gitx.provision_worktree(self.repo, scratch, [])  # crashed after step 1: registered and on disk
        self.assertEqual(gitx.registration(self.repo, scratch), 'active')
        c = self.claim(tid, 'bob')
        self.assertEqual(c['epoch'], 2)
        self.assertFalse(os.path.lexists(scratch))
        self.assertEqual(gitx.registration(self.repo, scratch), 'absent')
        self.assertTrue(Path(c['worktree']).is_dir())
        self.assertEqual(self.state(tid), 'claimed')
        self.assertEqual({w.path: w.status for w in model.load_worktrees(self.conn, tid)},
                         {str(scratch): 'retired', c['worktree']: 'active'})


class CleanupTests(Base):
    def test_cleanup_never_discards(self):
        tid, c, s = self.submitted()
        integrate.verify(self.conn, self.repo, tid, 'tester')
        wt = Path(c['worktree'])
        write(wt, 'extra.txt', 'keep me\n')
        e = self.refuse('unclean_worktree', tasks.cleanup, self.conn, self.repo, tid)
        self.assertEqual(e.detail['paths'], ['extra.txt'])
        self.assertEqual(e.detail['worktree'], str(wt))
        os.unlink(wt / 'extra.txt')
        os.symlink('/etc/hosts', wt / 'extra.txt')
        self.assertEqual(self.refuse('unclean_worktree', tasks.cleanup, self.conn, self.repo, tid).detail['paths'],
                         ['extra.txt'])
        os.unlink(wt / 'extra.txt')
        write(wt, '__pycache__/x.pyc', b'\x00cache')
        self.assertEqual(self.refuse('unclean_worktree', tasks.cleanup, self.conn, self.repo, tid).detail['paths'],
                         ['__pycache__/x.pyc'])
        os.chmod(wt / 'README.md', 0o755)  # mode-only change is also unaccounted
        self.assertEqual(self.refuse('unclean_worktree', tasks.cleanup, self.conn, self.repo, tid).detail['paths'],
                         ['README.md', '__pycache__/x.pyc'])
        os.chmod(wt / 'README.md', 0o644)
        shutil.rmtree(wt / '__pycache__')
        self.assertTrue(wt.is_dir())
        self.assertEqual(tasks.cleanup(self.conn, self.repo, tid), [wt])
        self.assertFalse(os.path.lexists(wt))
        self.assertEqual(gitx.registration(self.repo, wt), 'absent')
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 6\n')

    def test_cleanup_accounts_only_the_accepted_bundle(self):
        tid, c, s = self.submitted()  # submission 1: src/a.py = VALUE = 6
        integrate.reject(self.conn, tid, 'not this one')
        wt = Path(c['worktree'])
        write(wt, 'src/a.py', 'VALUE = 4\n')
        s2 = tasks.submit(self.conn, self.repo, tid, 'alice', c['epoch'])
        self.assertNotEqual(s2['submission_id'], s['submission_id'])
        integrate.verify(self.conn, self.repo, tid, 'tester')
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 4\n')
        write(wt, 'src/a.py', 'VALUE = 6\n')  # the rejected submission's content: never published
        e = self.refuse('unclean_worktree', tasks.cleanup, self.conn, self.repo, tid)
        self.assertEqual(e.detail['paths'], ['src/a.py'])
        write(wt, 'src/a.py', 'VALUE = 4\n')  # the accepted after-state is accounted
        self.assertEqual(tasks.cleanup(self.conn, self.repo, tid), [wt])

    def test_cleanup_refuses_active_task_but_allows_retired_worktree(self):
        tid = self.task('src/a.py')
        c1 = self.claim(tid, 'alice')
        self.refuse('bad_state', tasks.cleanup, self.conn, self.repo, tid)
        write(c1['worktree'], 'src/a.py', 'VALUE = 6\n')
        self.expire()
        c2 = self.claim(tid, 'bob')
        removed = tasks.cleanup(self.conn, self.repo, tid)  # retired worktree: transferred content is accounted
        self.assertEqual(removed, [Path(c1['worktree'])])
        self.assertTrue(Path(c2['worktree']).is_dir())


class RecoverTests(Base):
    def test_live_provisioning_next_action_is_one_release_refuses(self):
        from wcp import recover
        tid = self.task('src/a.py')
        with db.write_txn(self.conn):
            epoch = db.next_epoch(self.conn)
            model.transition(self.conn, model.load_task(self.conn, tid), 'provisioning', owner='ghost',
                             epoch=epoch, lease_expires_at=db.now() + 600,
                             worktree_path=str(db.repo_dir(self.repo) / 'worktrees' / ('%s-e%d' % (tid, epoch))))
        nxt = recover.recover(self.conn, self.repo, tid, db.now())['next_action']
        self.refuse('bad_state', tasks.release, self.conn, tid)  # what the advice must not send you to
        self.assertNotIn('wcp task release', nxt)
        self.assertIn('wait', nxt)
        self.assertIn('wcp task claim %s' % tid, nxt)

    def test_next_action_strings(self):
        from wcp import recover
        tid = self.task('src/a.py')
        c = self.claim(tid)
        write(c['worktree'], 'src/a.py', 'VALUE = 6\n')
        self.expire()
        r = recover.recover(self.conn, self.repo, tid, db.now())
        self.assertTrue(r['lease'].startswith('expired'))
        self.assertIn('reclaim', r['next_action'])
        self.assertIn('wcp task claim %s' % tid, r['next_action'])
        self.assertIn('1 changed file', r['next_action'])
        self.assertTrue(r['worktree_exists'])
        self.offset = 0.0
        tasks.submit(self.conn, self.repo, tid, 'alice', c['epoch'])
        r = recover.recover(self.conn, self.repo, tid, db.now())
        self.assertEqual(r['state'], 'submitted')
        self.assertIn('wcp task verify %s' % tid, r['next_action'])
        self.assertEqual(r['submission']['changes'], ['src/a.py'])
        rc, out = self.cli('task', 'recover', tid)
        self.assertEqual(rc, 0)
        self.assertEqual(out['next_action'], r['next_action'])


class WaitingTests(Base):
    """Addendum A1/A2: ready reports, claim --wait polls db.sleep(2) around the unchanged atomic claim."""

    def _accept(self, tid, c, rel='src/a.py', text='VALUE = 6\n'):
        write(c['worktree'], rel, text)
        tasks.submit(self.conn, self.repo, tid, c['task'].owner, c['epoch'])
        integrate.verify(self.conn, self.repo, tid, 'tester')

    def test_ready_reports_waiting_on_and_claim_wait_serializes(self):
        a = self.task('src/a.py')
        ca = self.claim(a, 'alice')
        b = tasks.create_task(self.conn, 'b', ('src/**',))
        rc, out = self.cli('task', 'ready', b.id)
        self.assertEqual((rc, out['state'], out['waiting_on']), (0, 'ready', [a]))
        sleeps = []
        with mock.patch.object(db, 'sleep', sleeps.append):
            e = self.refuse('scope_overlap', self.claim, b.id, 'bob')  # no --wait: refuses immediately
            self.assertEqual(e.detail['waiting_on'], [a])
            self.assertEqual(sleeps, [])
            self.assertEqual(self.state(b.id), 'ready')

        def sleep(seconds):  # the world moves while B waits: A finishes on the second poll
            sleeps.append(seconds)
            if len(sleeps) == 2:
                self._accept(a, ca)

        with mock.patch.object(db, 'sleep', sleep):
            cb = tasks.claim_task(self.conn, self.repo, b.id, 'bob', wait_s=-1)
        self.assertEqual(sleeps, [2, 2])
        self.assertEqual((self.state(a), cb['task'].state, cb['epoch']), ('accepted', 'claimed', 2))
        self.assertEqual((Path(cb['worktree']) / 'src/a.py').read_text(), 'VALUE = 6\n')  # claimed after A landed

    def test_claim_wait_timeout_carries_waiting_on(self):
        a = self.task('src/a.py')
        self.claim(a, 'alice')
        b = self.task('src/**')
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            self.offset += seconds

        with mock.patch.object(db, 'sleep', sleep):
            e = self.refuse('waiting_timeout', tasks.claim_task, self.conn, self.repo, b, 'bob', wait_s=5)
        self.assertEqual((e.detail['waiting_on'], e.detail['task_id']), ([a], b))
        self.assertEqual(sleeps, [2, 2, 2])
        self.assertEqual(self.state(b), 'ready')
        self.assertEqual(db.meta_get(self.conn, 'epoch'), '1')  # waiting consumed nothing
        rc, out = self.cli('task', 'claim', b, '--owner', 'bob', '--wait', '0')
        self.assertEqual((rc, out['code'], out['detail']['waiting_on']), (3, 'scope_overlap', [a]))

    def test_claim_wait_on_dependency(self):
        a = self.task('src/a.py')
        ca = self.claim(a, 'alice')
        b = self.task('src/b.py', depends=(a,))
        e = self.refuse('deps_unmet', self.claim, b, 'bob')
        self.assertEqual(e.detail['waiting_on'], [a])
        with mock.patch.object(db, 'sleep', lambda s: self._accept(a, ca)):
            cb = tasks.claim_task(self.conn, self.repo, b, 'bob', wait_s=30)
        self.assertEqual(cb['task'].state, 'claimed')
        self.assertEqual(tasks.waiting_on(self.conn, b, db.now()), [])
        # other refusals never wait
        with mock.patch.object(db, 'sleep', lambda s: self.fail('slept on a live lease')):
            self.refuse('bad_state', tasks.claim_task, self.conn, self.repo, b, 'carol', wait_s=-1)


class RebaseTests(Base):
    """Addendum A4/A5: the owner's own reclaim, durable rebase obligations, rebase_pending at submit."""

    def _drifted(self, submit=True):
        """alice edits a.py=6 and b.py=4 under src/**; canonical a.py moves to 7 underneath."""
        tid = self.task('src/**')
        c = self.claim(tid, 'alice')
        tasks.heartbeat(self.conn, tid, 'alice', c['epoch'])
        write(c['worktree'], 'src/a.py', 'VALUE = 6\n')
        write(c['worktree'], 'src/b.py', 'VALUE = 4\n')
        if submit:
            tasks.submit(self.conn, self.repo, tid, 'alice', c['epoch'])
        write(self.repo, 'src/a.py', 'VALUE = 7\n')
        return tid, c

    def test_rebase_after_drift_transfers_records_obligation_and_gates_submit(self):
        tid, c1 = self._drifted()
        self.refuse('before_hash_drift', integrate.verify, self.conn, self.repo, tid, 'tester')
        self.assertEqual(self.state(tid), 'conflicted')
        r = tasks.rebase_task(self.conn, self.repo, tid, 'alice', c1['epoch'])
        self.assertEqual((r['epoch'], r['task'].state, r['task'].owner, r['retired_worktree']),
                         (2, 'running', 'alice', c1['worktree']))
        self.assertNotEqual(r['worktree'], c1['worktree'])
        self.assertEqual([x['path'] for x in r['recovered']], ['src/b.py'])
        self.assertEqual([(x['path'], x['retired_worktree']) for x in r['rebase_needed']], [('src/a.py', c1['worktree'])])
        self.assertEqual((Path(r['worktree']) / 'src/a.py').read_text(), 'VALUE = 7\n')  # canonical's, not copied
        self.assertEqual((Path(r['worktree']) / 'src/b.py').read_text(), 'VALUE = 4\n')  # transferred
        self.assertTrue(Path(c1['worktree']).is_dir())
        self.assertEqual((Path(c1['worktree']) / 'src/a.py').read_text(), 'VALUE = 6\n')  # retained, untouched
        self.assertEqual({w.path: w.status for w in model.load_worktrees(self.conn, tid)},
                         {c1['worktree']: 'retired', r['worktree']: 'active'})
        self.assertEqual([(o.path, o.status, o.retired_worktree) for o in model.load_obligations(self.conn, tid, 2)],
                         [('src/a.py', 'pending', c1['worktree'])])
        self.assertFalse(model.load_task(self.conn, tid).lease_expired(db.now()))
        rc, out = self.cli('task', 'show', tid)
        self.assertEqual([o['path'] for o in out['rebase_obligations']], ['src/a.py'])
        # the old submission is superseded by the epoch; the new epoch cannot submit around the obligation
        self.refuse('stale_epoch', tasks.submit, self.conn, self.repo, tid, 'alice', c1['epoch'])
        e = self.refuse('rebase_pending', tasks.submit, self.conn, self.repo, tid, 'alice', 2)
        self.assertEqual((e.detail['paths'], e.detail['retired_worktree'], e.detail['epoch']),
                         (['src/a.py'], c1['worktree'], 2))
        self.assertEqual(self.state(tid), 'running')
        self.assertEqual(tasks.latest_submission(self.conn, tid).epoch, 1)  # nothing new was stored
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 7\n')
        # the builder re-applies against the new baseline: auto-resolved at submit, then verify publishes
        write(r['worktree'], 'src/a.py', 'VALUE = 6\n')
        s = tasks.submit(self.conn, self.repo, tid, 'alice', 2)
        self.assertEqual(sorted(ch['path'] for ch in s['changes']), ['src/a.py', 'src/b.py'])
        self.assertEqual([o.status for o in model.load_obligations(self.conn, tid, 2)], ['resolved'])
        v = integrate.verify(self.conn, self.repo, tid, 'tester')
        self.assertEqual((v['published'], v['state']), (True, 'accepted'))
        self.assertEqual((self.canonical('src/a.py'), self.canonical('src/b.py')), ('VALUE = 6\n', 'VALUE = 4\n'))
        kinds = [(r_['kind'], json.loads(r_['detail_json']).get('kind')) for r_ in self.conn.execute(
            "SELECT kind, detail_json FROM events WHERE task_id=? AND kind='retired'", (tid,))]
        self.assertEqual(kinds, [('retired', 'rebase')])

    def test_rebase_then_ack_keep_publishes_canonicals_version(self):
        tid, c1 = self._drifted(submit=False)
        rc, r = self.cli('task', 'rebase', tid, '--owner', 'alice', '--epoch', c1['epoch'])
        self.assertEqual((rc, r['epoch'], r['state']), (0, 2, 'running'), r)
        self.assertEqual([x['path'] for x in r['rebase_needed']], ['src/a.py'])
        rc, out = self.cli('task', 'ack', tid, '--owner', 'alice', '--epoch', 2, '--keep', 'src/a.py')
        self.assertEqual((rc, out['kept'], out['pending']), (0, 'src/a.py', []))
        self.assertEqual([o.status for o in model.load_obligations(self.conn, tid, 2)], ['kept'])
        self.refuse('not_found', tasks.ack_obligation, self.conn, tid, 'alice', 2, 'src/a.py')  # nothing pending
        self.refuse('not_found', tasks.ack_obligation, self.conn, tid, 'alice', 2, 'src/b.py')
        self.refuse('not_owner', tasks.ack_obligation, self.conn, tid, 'mallory', 2, 'src/a.py')
        self.refuse('stale_epoch', tasks.ack_obligation, self.conn, tid, 'alice', 1, 'src/a.py')
        s = tasks.submit(self.conn, self.repo, tid, 'alice', 2)
        self.assertEqual([ch['path'] for ch in s['changes']], ['src/b.py'])
        self.assertTrue(integrate.verify(self.conn, self.repo, tid, 'tester')['published'])
        self.assertEqual((self.canonical('src/a.py'), self.canonical('src/b.py')), ('VALUE = 7\n', 'VALUE = 4\n'))
        self.assertEqual((Path(c1['worktree']) / 'src/a.py').read_text(), 'VALUE = 6\n')  # still retained

    def test_obligation_resolves_only_with_a_stored_submission(self):
        tid = self.task('src/**', checks=('python3 tests/check.py',))
        c1 = self.claim(tid, 'alice')
        tasks.heartbeat(self.conn, tid, 'alice', c1['epoch'])
        write(c1['worktree'], 'src/a.py', 'VALUE = 6\n')
        write(self.repo, 'src/a.py', 'VALUE = 7\n')
        r = tasks.rebase_task(self.conn, self.repo, tid, 'alice', c1['epoch'])
        write(r['worktree'], 'src/a.py', 'VALUE = 7  # touched\n')
        write(r['worktree'], 'src/b.py', 'VALUE = 5  # 12 > 11\n')
        self.assertEqual(self.canonical('src/b.py'), 'VALUE = 5\n')
        self.refuse('checks_failed', tasks.submit, self.conn, self.repo, tid, 'alice', 2)
        self.assertEqual([o.status for o in model.load_obligations(self.conn, tid, 2)], ['pending'])
        write(r['worktree'], 'src/a.py', 'VALUE = 7\n')  # reverted to baseline: the obligation bites again
        self.refuse('rebase_pending', tasks.submit, self.conn, self.repo, tid, 'alice', 2)
        write(r['worktree'], 'src/a.py', 'VALUE = 6\n')
        tasks.submit(self.conn, self.repo, tid, 'alice', 2)
        self.assertEqual([o.status for o in model.load_obligations(self.conn, tid, 2)], ['resolved'])

    def test_rebase_refusals(self):
        tid = self.task('src/a.py')
        c = self.claim(tid, 'alice')
        self.refuse('bad_state', tasks.rebase_task, self.conn, self.repo, tid, 'alice', c['epoch'])  # claimed
        tasks.heartbeat(self.conn, tid, 'alice', c['epoch'])
        self.refuse('not_owner', tasks.rebase_task, self.conn, self.repo, tid, 'mallory', c['epoch'])
        self.refuse('stale_epoch', tasks.rebase_task, self.conn, self.repo, tid, 'alice', c['epoch'] + 1)
        tasks.release(self.conn, tid)
        self.refuse('stale_epoch', tasks.rebase_task, self.conn, self.repo, tid, 'alice', c['epoch'])
        c2 = self.claim(tid, 'alice')
        tasks.heartbeat(self.conn, tid, 'alice', c2['epoch'])
        self.expire()
        self.refuse('lease_expired', tasks.rebase_task, self.conn, self.repo, tid, 'alice', c2['epoch'])
        self.assertEqual(db.meta_get(self.conn, 'epoch'), '3')  # no refusal consumed an epoch
        self.assertEqual(len(model.load_worktrees(self.conn, tid)), 2)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['--repo', str(self.repo), 'task', 'ack', tid, '--owner', 'a', '--epoch', '1']), 2)


class RunnerReclaimTests(Base):
    """Addendum B6: a run reclaims its own task on a live lease (runner.lock proves the old runner is gone)."""

    def test_own_owner_live_lease_reclaims_and_transfers(self):
        tid = self.task('src/a.py')
        c1 = self.claim(tid, 'run:r-1:k')
        write(c1['worktree'], 'src/a.py', 'VALUE = 6\n')
        self.assertFalse(model.load_task(self.conn, tid).lease_expired(db.now()))
        r = tasks.runner_reclaim(self.conn, self.repo, tid, 'run:r-1:k', 60)
        self.assertEqual((r['epoch'], r['task'].owner, r['task'].state), (2, 'run:r-1:k', 'claimed'))
        self.assertEqual([x['path'] for x in r['recovered']], ['src/a.py'])
        self.assertEqual({w.path: w.status for w in model.load_worktrees(self.conn, tid)},
                         {c1['worktree']: 'retired', r['worktree']: 'active'})
        self.assertAlmostEqual(r['lease_expires_at'], db.now() + 60, delta=5)
        self.refuse('stale_epoch', tasks.heartbeat, self.conn, tid, 'run:r-1:k', c1['epoch'])

    def test_foreign_live_lease_refused_expired_lease_reclaimed(self):
        tid = self.task('src/a.py')
        self.claim(tid, 'alice')
        self.refuse('bad_state', tasks.runner_reclaim, self.conn, self.repo, tid, 'run:r-1:k', 60)
        t2 = self.task('src/b.py')
        self.claim(t2, 'run:r-2:k')
        self.refuse('bad_state', tasks.runner_reclaim, self.conn, self.repo, t2, 'run:r-1:k', 60)  # another run
        self.assertEqual(db.meta_get(self.conn, 'epoch'), '2')
        self.expire()
        r = tasks.runner_reclaim(self.conn, self.repo, tid, 'run:r-1:k', 60)
        self.assertEqual((r['epoch'], r['task'].owner), (3, 'run:r-1:k'))

    def test_reclaims_from_provisioning_and_submitted_and_records_obligations(self):
        tid = self.task('src/a.py')
        scratch = db.repo_dir(self.repo) / 'worktrees' / ('%s-e1' % tid)
        with db.write_txn(self.conn):
            epoch = db.next_epoch(self.conn)
            model.transition(self.conn, model.load_task(self.conn, tid), 'provisioning', owner='run:r-1:k',
                             epoch=epoch, lease_expires_at=db.now() + 600, worktree_path=str(scratch))
            self.conn.execute("INSERT INTO worktrees(path, task_id, epoch, status, created_at) VALUES (?,?,?,'provisioning',?)",
                              (str(scratch), tid, epoch, db.now()))
        db.ensure_private(scratch.parent, True)
        gitx.provision_worktree(self.repo, scratch, [])  # the previous runner died after step 1
        self.refuse('bad_state', self.claim, tid, 'run:r-1:k')  # a plain claim still waits for the lease
        r = tasks.runner_reclaim(self.conn, self.repo, tid, 'run:r-1:k', 60)
        self.assertEqual((r['epoch'], r['task'].state), (2, 'claimed'))
        self.assertFalse(os.path.lexists(scratch))
        # from submitted on a live lease, with canonical moved: obligation recorded, old submission fenced
        write(r['worktree'], 'src/a.py', 'VALUE = 6\n')
        tasks.submit(self.conn, self.repo, tid, 'run:r-1:k', 2)
        write(self.repo, 'src/a.py', 'VALUE = 7\n')
        r2 = tasks.runner_reclaim(self.conn, self.repo, tid, 'run:r-1:k', 60)
        self.assertEqual((r2['epoch'], r2['task'].state, r2['retired_worktree']), (3, 'claimed', r['worktree']))
        self.assertEqual([(o.path, o.status) for o in model.load_obligations(self.conn, tid, 3)], [('src/a.py', 'pending')])
        self.assertEqual(tasks.latest_submission(self.conn, tid).epoch, 2)  # superseded: fenced by the epoch
        self.refuse('bad_state', integrate.verify, self.conn, self.repo, tid, 'tester')  # claimed, not submitted
        self.refuse('rebase_pending', tasks.submit, self.conn, self.repo, tid, 'run:r-1:k', 3)
        for state in ('verifying', 'publishing', 'accepted'):  # integrator-owned or done: never reclaimable
            with db.write_txn(self.conn):
                self.conn.execute('UPDATE tasks SET state=?, version=version+1 WHERE id=?', (state, tid))
            self.refuse('bad_state', tasks.runner_reclaim, self.conn, self.repo, tid, 'run:r-1:k', 60)
        self.assertEqual(db.meta_get(self.conn, 'epoch'), '3')


class CreateInTxnTests(Base):
    def test_requires_txn_and_rolls_back_with_caller(self):
        with self.assertRaises(RuntimeError):
            tasks.create_task_in_txn(self.conn, 'g', ('src/a.py',))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)
        with self.assertRaises(ValueError):
            with db.write_txn(self.conn):
                t1 = tasks.create_task_in_txn(self.conn, 'first', ('src/a.py',))
                self.assertEqual(model.load_task(self.conn, t1.id).state, 'proposed')
                raise ValueError('the run failed on a later task')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM task_scopes').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM events').fetchone()[0], 0)
        e = None
        with self.assertRaises(ProtocolError) as cm:
            with db.write_txn(self.conn):
                tasks.create_task_in_txn(self.conn, 'first', ('src/a.py',), depends=('t-later',))
                tasks.create_task_in_txn(self.conn, 'second', ('config/.env',))  # refused: everything rolls back
        self.assertEqual(cm.exception.code, 'sensitive_path')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)
        self.assertFalse(self.conn.in_transaction)
        with db.write_txn(self.conn):
            a = tasks.create_task_in_txn(self.conn, 'a', ('src/a.py',))
            b = tasks.create_task_in_txn(self.conn, 'b', ('src/b.py',), depends=(a.id,), checks=('true',))
        self.assertEqual(model.load_deps(self.conn, b.id), [a.id])
        self.assertEqual(tasks.ready_task(self.conn, b.id).state, 'ready')
        self.assertEqual(tasks.waiting_on(self.conn, b.id, db.now()), [a.id])


class CliTests(Base):
    def test_usage_error_exits_2(self):
        with mock.patch.object(tasks, 'ready_task', side_effect=ProtocolError('usage', 'bad plan')):
            rc, out = self.cli('task', 'ready', 't-000000')
        self.assertEqual((rc, out['ok'], out['code']), (2, False, 'usage'))

    def test_exit_codes_and_json_shape(self):
        rc, out = self.cli('task', 'show', 't-nope')
        self.assertEqual(rc, 3)
        self.assertEqual((out['ok'], out['code']), (False, 'not_found'))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['--repo', str(self.repo), 'task']), 2)
            self.assertEqual(cli.main(['--repo', str(self.repo), 'task', 'claim', 'x']), 2)  # --owner missing
        rc, out = self.cli('task', 'create', '--goal', 'g', '--write', 'src/a.py', '--resource', 'port:1')
        self.assertEqual(rc, 0)
        tid = out['id']
        self.assertEqual(self.cli('task', 'ready', tid)[1]['state'], 'ready')
        rc, c = self.cli('task', 'claim', tid, '--owner', 'alice', '--lease', '60')
        self.assertEqual(rc, 0)
        self.assertEqual(c['codex_hint'], 'codex -C %s -s workspace-write' % c['worktree'])
        rc, out = self.cli('task', 'submit', tid, '--owner', 'alice', '--epoch', c['epoch'])
        self.assertEqual((rc, out['code']), (3, 'bad_state'))  # nothing changed
        rc, out = self.cli('ps')
        self.assertEqual(out['tasks'][0]['id'], tid)
        rc, out = self.cli('status')
        self.assertEqual(out['active_tasks'][0]['id'], tid)
        self.assertEqual(out['required_checks'][0]['cmd'], 'python3 tests/check.py')
        rc, out = self.cli('events', tid)
        self.assertEqual(out['events'][0]['kind'], 'create')
        self.assertEqual(self.cli('task', 'recover', tid)[0], 0)
        self.assertEqual(self.cli('task', 'show', tid)[1]['scope'], {'path': ['src/a.py'], 'resource': ['port:1']})
        # human output does not crash
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(['--repo', str(self.repo), 'ps']), 0)
            self.assertEqual(cli.main(['--repo', str(self.repo), 'task', 'recover', tid]), 0)
            self.assertEqual(cli.main(['--repo', str(self.repo), 'status']), 0)
        self.assertIn(tid, buf.getvalue())
        self.assertIn('next_action:', buf.getvalue())

    def test_init_reads_agent_tools_and_links(self):
        write(self.repo, '.agent-tools.json', json.dumps({
            'checks': [{'id': 'unit', 'argv': ['python3', 'tests/check.py'], 'required': True},
                       {'id': 'skip', 'cmd': 'false', 'required': True, 'skip': True}],
            'completion': {'required_check_ids': ['unit', 'skip']}, 'preserve_roots': ['vendor']}))
        rc, out = self.cli('init')
        self.assertEqual(out['required_checks'], [{'argv': ['python3', 'tests/check.py'], 'cmd': None, 'cwd': '.',
                                                   'timeout_s': 900}])
        self.assertEqual(out['preserve_roots'], ['vendor'])
        self.assertEqual(out['link_dirs'], [])
        os.mkdir(self.repo / 'node_modules')
        rc, out = self.cli('init', '--link', 'node_modules')
        self.assertEqual(out['link_dirs'], ['node_modules'])
        self.assertIn('link_caveat', out)
        tid = self.task('src/a.py')
        c = self.claim(tid)
        self.assertTrue(os.path.islink(Path(c['worktree']) / 'node_modules'))
        self.assertEqual(self.cli('init', '--no-link')[1]['link_dirs'], [])
        self.refuse('preserve_root', tasks.create_task, self.conn, 'x', ('vendor/**',))

    def test_owner_validation_rejects_unsafe_owners(self):
        tid = self.task('src/a.py')
        for bad in ('', 'alice bob', '-flag', '--help', '../evil', 'alice/bob', 'alice;id', 'a$b', 'alice\n', 'x' * 129):
            with self.subTest(bad=bad):
                rc, out = self.cli('task', 'claim', tid, '--owner=%s' % bad)
                self.assertEqual((rc, out['ok'], out['code']), (2, False, 'usage'))
                self.refuse('usage', tasks.claim_task, self.conn, self.repo, tid, bad)
        for good in ('alice', 'racer-1', 'claude:session-a1b2', 'orch:worker_1.2', 'run:r-1a2b3c:key'):
            self.assertEqual(tasks.validate_owner(good), good)

    def test_shell_quoting_in_wcp_cmd_and_codex_hint(self):
        import shlex
        fake_dir = self.home / 'dir with spaces & $vars'
        (fake_dir / 'bin').mkdir(parents=True)
        launcher = fake_dir / 'bin' / 'wcp'
        launcher.write_text('#!/bin/sh\n')
        with mock.patch.object(cli, '__file__', str(fake_dir / 'wcp' / 'cli.py')):
            cmd = cli._wcp_cmd()
            self.assertEqual(cmd, shlex.quote(str(launcher)))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(['hook', 'print', '--claude'])
            self.assertEqual(rc, 0)
            printed = json.loads(buf.getvalue())
            self.assertEqual(printed['hooks']['PreToolUse'][0]['hooks'][0]['command'],
                             '%s hook pretooluse' % shlex.quote(str(launcher)))
        spaced_home = self.home / 'Application Support' / 'wcp'
        spaced_home.mkdir(parents=True)
        os.chmod(spaced_home, 0o700)
        old_home = os.environ['WCP_HOME']
        os.environ['WCP_HOME'] = str(spaced_home)
        try:
            conn2 = db.open_db(self.repo)
            try:
                t = tasks.create_task(conn2, 'goal', ('src/a.py',))
                tasks.ready_task(conn2, t.id)
                c = tasks.claim_task(conn2, self.repo, t.id, 'alice')
                self.assertEqual(c['codex_hint'], 'codex -C %s -s workspace-write' % shlex.quote(c['worktree']))
                self.assertEqual(shlex.split(c['codex_hint']), ['codex', '-C', c['worktree'], '-s', 'workspace-write'])
            finally:
                conn2.close()
        finally:
            os.environ['WCP_HOME'] = old_home


if __name__ == '__main__':
    unittest.main()
