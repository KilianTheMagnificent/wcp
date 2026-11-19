"""Addendum tests 4-10 for `wcp run`, plus plan validation, brief determinism, run.jsonl/DB hygiene,
the --json summary shape, and the runner lock. Builders are the scripts in tests/fake_builders/."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fixtures import commit, write
from test_tasks import Base
from wcp import cli, db, gitx, integrate, model, run, tasks
from wcp.db import ProtocolError

ROOT = Path(__file__).resolve().parents[1]
FAKES = Path(__file__).resolve().parent / 'fake_builders'
SIX, FOUR, SEVEN, HUMAN = 'VALUE = 6\n', 'VALUE = 4\n', 'VALUE = 7\n', 'VALUE = 5  # touched by a human\n'
SK = 'sk-' + 'a' * 30
PEM = '-----BEGIN RSA PRIVATE KEY-----'


def plan(builders, task_list, **top):
    p = {'version': 1, 'builders': builders, 'tasks': task_list}
    p.update(top)
    return p


def task(key, write_=(), builder='fake', **kw):
    t = {'key': key, 'goal': 'goal of ' + key, 'write': list(write_), 'builder': builder}
    t.update(kw)
    return t


class PlanTests(unittest.TestCase):
    B = {'fake': {'argv': ['python3', 'x.py']}}

    def good(self):
        return plan(dict(self.B), [task('a', ['src/a.py']), task('b', ['src/b.py'], depends=['a'], check=['true'])])

    def usage(self, mutate, msg=''):
        p = self.good()
        mutate(p)
        with self.assertRaises(ProtocolError, msg=msg) as cm:
            run.validate_plan(p)
        self.assertEqual(cm.exception.code, 'usage')

    def test_defaults_and_normalization(self):
        self.assertIs(run.PLAN_DEFAULTS['auto_approve_scope'], False)
        p = self.good()
        p['tasks'][0]['write'] = ['src/a.py', 'docs/x.md']
        out = run.validate_plan(p)
        self.assertEqual((out['max_concurrent'], out['max_rebase'], out['max_attempts'], out['auto_approve_scope'],
                          out['lease_s']), (3, 2, 2, False, 900))
        self.assertEqual(out['builders']['fake']['timeout_s'], 1800)
        self.assertEqual(out['tasks'][0]['write'], ['src/a.py', 'docs/x.md'])
        self.assertEqual(out['tasks'][1], {'key': 'b', 'goal': 'goal of b', 'builder': 'fake', 'brief': '',
                                           'write': ['src/b.py'], 'resource': [], 'depends': ['a'], 'check': ['true']})

    def test_refusal_table(self):
        self.usage(lambda p: p.update(extra=1), 'unknown top key')
        self.usage(lambda p: p.update(version=2), 'version')
        self.usage(lambda p: p['tasks'][0].update(builder='nope'), 'unknown builder')
        self.usage(lambda p: p['tasks'][1].update(depends=['zz']), 'unknown depends')
        self.usage(lambda p: p['tasks'][1].update(key='a'), 'duplicate key')
        self.usage(lambda p: p['tasks'][0].update(write=['.env']), 'sensitive pattern')
        self.usage(lambda p: p['tasks'][0].update(write=['../x']), 'unsafe pattern')
        self.usage(lambda p: p['tasks'][0].update(write='src/a.py'), 'non-list write')
        self.usage(lambda p: p['tasks'][0].update(depends=['b']), 'cycle')
        self.usage(lambda p: p['tasks'][0].update(depends=['a']), 'self dependency')
        self.usage(lambda p: p['tasks'][0].update(surprise=1), 'unknown task key')
        self.usage(lambda p: p['tasks'][0].update(key='a b'), 'bad key chars')
        self.usage(lambda p: p['tasks'][0].update(goal=''), 'empty goal')
        self.usage(lambda p: p.update(tasks=[]), 'no tasks')
        self.usage(lambda p: p.update(builders={}), 'no builders')
        self.usage(lambda p: p['builders'].update(fake={'argv': []}), 'empty argv')
        self.usage(lambda p: p['builders'].update(fake={'argv': ['x'], 'shell': True}), 'unknown builder key')
        self.usage(lambda p: p['builders'].update(fake={'argv': ['x'], 'timeout_s': 0}), 'timeout')
        self.usage(lambda p: p.update(max_attempts=0), 'max_attempts')
        self.usage(lambda p: p.update(max_rebase=-1), 'max_rebase')
        self.usage(lambda p: p.update(max_concurrent=True), 'bool is not int')
        self.usage(lambda p: p.update(auto_approve_scope='yes'), 'auto_approve_scope')
        with self.assertRaises(ProtocolError) as cm:
            run.validate_plan([])
        self.assertEqual(cm.exception.code, 'usage')

    def test_load_plan_bad_file(self):
        d = tempfile.mkdtemp(prefix='wcp-plan-')
        self.addCleanup(shutil.rmtree, d, True)
        bad = Path(d) / 'plan.json'
        bad.write_text('{not json')
        for path in (bad, Path(d) / 'missing.json'):
            with self.assertRaises(ProtocolError) as cm:
                run.load_plan(path)
            self.assertEqual(cm.exception.code, 'usage')

    def test_builder_env_strips_every_wcp_var(self):
        base = {'HOME': '/h', 'PATH': '/bin', 'USER': 'u', 'LANG': 'C.UTF-8', 'TMPDIR': '/t', 'SHELL': '/bin/sh',
                'WCP_ROLE': 'integrator', 'WCP_FOO': 'x', 'WCP_HOME': '/elsewhere',
                'AWS_SECRET_ACCESS_KEY': 'secret', 'GITHUB_TOKEN': 'ghp_secret', 'PYTHONPATH': '/evil',
                'DYLD_INSERT_LIBRARIES': '/evil.dylib', 'LD_PRELOAD': '/evil.so', 'GIT_SSH_COMMAND': 'evil'}
        ctx = {'task_id': 't-1', 'owner': 'run:r-1:a', 'epoch': 3, 'worktree': '/wt', 'key': 'a'}
        env = run.builder_env(base, ctx)
        self.assertEqual({k for k in env if k.startswith('WCP_')}, set(run.ENV_VARS))
        self.assertEqual(set(env) - set(run.ENV_VARS), {'HOME', 'PATH', 'USER', 'LANG', 'TMPDIR', 'SHELL'})
        self.assertEqual((env['WCP_ROLE'], env['WCP_EPOCH'], env['HOME'], env['PATH']), ('builder', '3', '/h', '/bin'))
        self.assertEqual(env['WCP_HOME'], str(db.DEFAULT_HOME()))
        with mock.patch.object(run, '_PKG_PARENT', '/tmp/my dir;echo pwned'), \
             mock.patch.object(sys, 'executable', '/opt/my python/bin/python3'):
            self.assertEqual(run.wcp_command(), "'/opt/my python/bin/python3' -m wcp")

    def test_brief_is_deterministic_and_complete(self):
        t = task('a', ['src/**', 'docs/a.md'], check=['python3 -m unittest'], brief='Be terse.', resource=['port:1'])
        ctx = {'key': 'a', 'task_id': 't-1', 'owner': 'run:r-1:a', 'epoch': 2, 'worktree': '/wt', 'root': '/repo',
               'wcp': '/repo/bin/wcp', 'obligations': [{'path': 'src/x.py', 'retired_worktree': '/old'}],
               'retired_worktree': '/old', 'failed_check': {'cmd': 'python3 tests/check.py', 'exit_code': 1},
               'previous_log': '/logs/builder-a-1.log', 'exit_code': 1, 'note': 'previous attempt failed'}
        one, two = run.brief_for(t, ctx), run.brief_for(t, dict(ctx))
        self.assertEqual(one, two)
        for needle in ('goal of a', 'docs/a.md', 'src/**', 'port:1', 'python3 -m unittest', 'Be terse.', '/repo/bin/wcp',
                       'scope request t-1 --owner run:r-1:a --epoch 2', 'SCOPE-NEEDED:', 'KEEP-CANONICAL:', 'src/x.py',
                       '/old', 'python3 tests/check.py', 'exited 1', '/logs/builder-a-1.log', 'Never touch', '/repo'):
            self.assertIn(needle, one)
        bare = run.brief_for(task('b', ['src/b.py']), {'key': 'b', 'task_id': 't-2', 'owner': 'o', 'epoch': 1,
                                                       'worktree': '/w', 'root': '/r', 'wcp': 'wcp'})
        self.assertNotIn('Rebase obligations', bare)
        self.assertNotIn('Previous attempt', bare)


class RunBase(Base):
    """Fixture repo + a control file for the fake builders; db.sleep shortened; stray builders killed."""

    def setUp(self):
        super().setUp()
        self.scratch = Path(tempfile.mkdtemp(prefix='wcp-run-')).resolve()
        self.addCleanup(shutil.rmtree, str(self.scratch), True)
        self.marks = self.scratch / 'marks'
        self.control = self.scratch / 'control.json'
        real_sleep = db.sleep
        self.addCleanup(setattr, db, 'sleep', real_sleep)
        db.sleep = lambda s: time.sleep(min(s, 0.02))
        self.addCleanup(setattr, run, 'KILL_GRACE_S', run.KILL_GRACE_S)
        run.KILL_GRACE_S = 2.0
        saved = {k: v for k, v in os.environ.items() if k.startswith('WCP_') and k != 'WCP_HOME'}
        for k in saved:
            del os.environ[k]
        self.addCleanup(os.environ.update, saved)
        self.addCleanup(self._kill_strays)

    def _kill_strays(self):
        for p in Path(self.home).glob('repos/*/runs/*/attempt-*.started.json'):
            try:
                os.killpg(int(json.loads(p.read_text())['pgid']), signal.SIGKILL)
            except (OSError, ValueError, KeyError):
                pass

    # -- plan plumbing ----------------------------------------------------------------------
    def ctl(self, **per_key):
        data = {'marks_dir': str(self.marks), 'root': str(self.repo)}
        data.update(per_key)
        self.control.write_text(json.dumps(data))
        return self.control

    def fake(self, script='edit.py', timeout_s=60, extra=()):
        return {'argv': [sys.executable, str(FAKES / script), str(self.control)] + list(extra), 'timeout_s': timeout_s}

    def start(self, p, **kw):
        r = run.Runner(self.conn, self.repo, plan=run.validate_plan(p), quiet=True, **kw)
        r.start()
        return r

    def run_plan(self, p, **kw):
        r = self.start(p, **kw)
        return r, r.loop()

    def resume(self, run_id, retry=(), retry_all=False, p=None, **kw):
        r = run.Runner(self.conn, self.repo, plan=run.validate_plan(p) if p else None, run_id=run_id, quiet=True, **kw)
        r.resume(retry, retry_all)
        return r, r.loop()

    def step_until_done(self, r, limit=200):
        """loop() one iteration at a time; returns the sequence of run_task statuses seen."""
        seen = []
        r.max_iterations = 1
        for _ in range(limit):
            s = r.loop()
            seen.append({rt['key']: rt['status'] for rt in r._rts()})
            if s['status'] in ('done', 'paused'):
                return s, seen
            time.sleep(0.05)
        self.fail('run did not finish')

    # -- observation ------------------------------------------------------------------------
    def events(self, r, key=None, kind=None):
        out = [json.loads(l) for l in (r.run_dir / 'run.jsonl').read_text().splitlines()]
        return [e for e in out if (key is None or e.get('key') == key) and (kind is None or e['event'] == kind)]

    def kinds(self, r, key):
        return [e['event'] for e in self.events(r, key)]

    def rt(self, r, key):
        return dict(r._rt(key))

    def task_of(self, r, key):
        return model.load_task(self.conn, self.rt(r, key)['task_id'])

    def marks_for(self, key):
        return sorted(p.name for p in self.marks.iterdir() if p.name.startswith(key + '-e')) if self.marks.is_dir() else []

    def attempts(self, r, key):
        return [dict(x) for x in self.conn.execute('SELECT * FROM run_attempts WHERE run_id=? AND key=? ORDER BY attempt',
                                                   (r.run_id, key))]

    def obligations(self, r, key):
        return [dict(x) for x in self.conn.execute('SELECT * FROM rebase_obligations WHERE task_id=? ORDER BY epoch, path',
                                                   (self.rt(r, key)['task_id'],))]

    def brief(self, r, key, n):
        return (r.run_dir / ('brief-%s-%d.md' % (key, n))).read_text()

    def log(self, r, key, n):
        return (r.run_dir / ('builder-%s-%d.log' % (key, n))).read_bytes()

    def db_dump(self):
        out = []
        for (name,) in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            out.extend(str(tuple(row)) for row in self.conn.execute('SELECT * FROM %s' % name))
        return '\n'.join(out)

    def run_tables(self):
        out = []
        for name in ('runs', 'run_tasks', 'run_attempts'):
            out.extend(str(tuple(row)) for row in self.conn.execute('SELECT * FROM %s' % name))
        return '\n'.join(out)

    def assert_nothing_leaked(self, r, *needles, whole_db=True):
        """Nothing of `needles` in run.jsonl, the briefs, or the whole DB (events included: verify_failed
        receipts carry no output_tail)."""
        blobs = {'run.jsonl': (r.run_dir / 'run.jsonl').read_text(),
                 'db': self.db_dump() if whole_db else self.run_tables()}
        for b in r.run_dir.glob('brief-*.md'):
            blobs[b.name] = b.read_text()
        for where, text in blobs.items():
            for n in needles:
                self.assertNotIn(n, text, '%r leaked into %s' % (n, where))


class StartTests(RunBase):
    def two(self):
        self.ctl(a={'attempts': [{'write': {'src/a.py': SIX}}]}, b={'attempts': [{'write': {'src/b.py': FOUR}}]})
        return plan({'fake': self.fake()}, [task('a', ['src/a.py']), task('b', ['src/b.py'])])

    def test_zero_check_repository_cannot_start_a_run(self):
        with db.write_txn(self.conn):
            db.meta_set(self.conn, 'required_checks', '[]')
        r = run.Runner(self.conn, self.repo, plan=run.validate_plan(self.two()), quiet=True)
        self.refuse('usage', r.start)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM runs').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)

    def test_run_creation_is_atomic(self):
        real = tasks.create_task_in_txn
        calls = []

        def flaky(conn, *a, **kw):
            calls.append(1)
            if len(calls) == 2:
                raise ProtocolError('preserve_root', 'injected on the last task', {})
            return real(conn, *a, **kw)

        with mock.patch.object(tasks, 'create_task_in_txn', flaky):
            r = run.Runner(self.conn, self.repo, plan=run.validate_plan(self.two()), quiet=True)
            self.refuse('preserve_root', r.start)
        for table in ('runs', 'run_tasks', 'tasks', 'task_scopes', 'events'):
            self.assertEqual(self.conn.execute('SELECT count(*) FROM %s' % table).fetchone()[0], 0, table)
        self.assertFalse(self.conn.in_transaction)

    def test_start_records_run_and_pending_rows(self):
        r = self.start(self.two())
        self.assertTrue(r.run_id.startswith('r-') and len(r.run_id) == 8)
        row = self.conn.execute('SELECT * FROM runs WHERE id=?', (r.run_id,)).fetchone()
        self.assertEqual((row['status'], row['root']), ('running', str(self.repo)))
        self.assertEqual([(x['key'], x['status'], x['attempts']) for x in r._rts()], [('a', 'pending', 0), ('b', 'pending', 0)])
        self.assertEqual(self.task_of(r, 'a').state, 'proposed')
        self.assertEqual(self.kinds(r, None)[:0], [])
        self.assertEqual([e['event'] for e in self.events(r)], ['created'])
        self.assertEqual(oct(r.run_dir.stat().st_mode & 0o777), '0o700')


class HappyPathTests(RunBase):
    def test_two_disjoint_tasks_accepted(self):
        self.ctl(a={'attempts': [{'write': {'src/a.py': SIX}}]}, b={'attempts': [{'write': {'src/b.py': FOUR}}]})
        r, s = self.run_plan(plan({'fake': self.fake()}, [task('a', ['src/a.py']), task('b', ['src/b.py'])]))
        self.assertEqual((s['status'], s['accepted'], s['paused']), ('done', ['a', 'b'], []))
        self.assertEqual((self.canonical('src/a.py'), self.canonical('src/b.py')), (SIX, FOUR))
        self.assertEqual(self.conn.execute('SELECT status FROM runs WHERE id=?', (r.run_id,)).fetchone()[0], 'done')
        for key in ('a', 'b'):
            self.assertEqual(self.kinds(r, key), ['dispatched', 'builder_exited', 'submitted', 'verify', 'published'])
            self.assertEqual(oct((r.run_dir / ('builder-%s-1.log' % key)).stat().st_mode & 0o777), '0o600')
            for suffix in ('started.json', 'exit.json', 'lock'):
                self.assertTrue((r.run_dir / ('attempt-%s-1.%s' % (key, suffix))).exists(), suffix)
            att = self.attempts(r, key)[0]
            self.assertEqual((att['exit_code'], att['task_epoch']), (0, self.task_of(r, key).epoch))
            self.assertIsNotNone(att['pgid'])
            self.assertEqual(json.loads((r.run_dir / ('attempt-%s-1.exit.json' % key)).read_text())['exit_code'], 0)
            self.assertEqual(self.task_of(r, key).owner, 'run:%s:%s' % (r.run_id, key))
        self.assertEqual([e['event'] for e in self.events(r)][0], 'created')
        self.assertEqual([e['event'] for e in self.events(r)][-1], 'done')
        self.assertEqual(len(self.marks_for('a')), 1)

    def test_dependency_orders_dispatch(self):
        self.ctl(a={'attempts': [{'write': {'src/a.py': SIX}}]}, b={'attempts': [{'write': {'src/b.py': FOUR}}]})
        r, s = self.run_plan(plan({'fake': self.fake()}, [task('a', ['src/a.py']), task('b', ['src/b.py'], depends=['a'])]))
        self.assertEqual(s['status'], 'done')
        ev = [(e['event'], e.get('key')) for e in self.events(r)]
        self.assertLess(ev.index(('published', 'a')), ev.index(('dispatched', 'b')))

    def test_max_concurrent_serializes(self):
        self.ctl(a={'attempts': [{'write': {'src/a.py': SIX}, 'sleep': 0.4}]}, b={'attempts': [{'write': {'src/b.py': FOUR}}]})
        r, s = self.run_plan(plan({'fake': self.fake()}, [task('a', ['src/a.py']), task('b', ['src/b.py'])], max_concurrent=1))
        self.assertEqual(s['status'], 'done')
        ev = [(e['event'], e.get('key')) for e in self.events(r)]
        self.assertLess(ev.index(('builder_exited', 'a')), ev.index(('dispatched', 'b')))

    def test_max_concurrent_does_not_admit_while_a_builder_is_being_reaped(self):
        """A builder seen alive during an iteration stays counted for the whole iteration: admitting on an
        observation fresher than the reap dispatches b before a's exit is recorded."""
        self.ctl(a={'attempts': [{'write': {'src/a.py': SIX}, 'sleep': 0.4}]}, b={'attempts': [{'write': {'src/b.py': FOUR}}]})
        real = run.Runner._check_timeout

        def slow(self_, rt, att):  # a exits between its own liveness check and b's admission check
            real(self_, rt, att)
            time.sleep(0.5)

        with mock.patch.object(run.Runner, '_check_timeout', slow):
            r, s = self.run_plan(plan({'fake': self.fake()}, [task('a', ['src/a.py']), task('b', ['src/b.py'])],
                                      max_concurrent=1))
        self.assertEqual(s['status'], 'done')
        ev = [(e['event'], e.get('key')) for e in self.events(r)]
        self.assertLess(ev.index(('builder_exited', 'a')), ev.index(('dispatched', 'b')))

    def _held_lock_run(self, steal=False):
        """Two quick builders, lease 3 s, the integrator flock held 6 s by someone else so a's verify blocks the
        loop past b's lease. steal: at 4.5 s a stranger tries to reclaim b on its (possibly lapsed) lease."""
        self.ctl(a={'attempts': [{'write': {'src/a.py': SIX}}]}, b={'attempts': [{'write': {'src/b.py': FOUR}}]})
        p = plan({'fake': self.fake()}, [task('a', ['src/a.py']), task('b', ['src/b.py'])], lease_s=3)
        release = threading.Event()
        stolen: dict = {}

        def hold():
            deadline = time.monotonic() + 6.0
            with db.integrator_lock(self.repo, 'someone-else'):
                if steal:
                    time.sleep(4.5)
                    conn = db.open_db(self.repo)
                    try:
                        tid = conn.execute("SELECT task_id FROM run_tasks WHERE key='b'").fetchone()[0]
                        try:
                            stolen['epoch'] = tasks.claim_task(conn, self.repo, tid, 'stranger')['epoch']
                        except ProtocolError as e:
                            stolen['code'] = e.code
                    finally:
                        conn.close()
                release.wait(max(0.0, deadline - time.monotonic()))

        t = threading.Thread(target=hold)
        t.start()
        self.addCleanup(t.join)
        self.addCleanup(release.set)
        time.sleep(0.1)
        try:
            r, s = self.run_plan(p)
        finally:
            release.set()
        t.join()
        return r, s, stolen

    def test_keepalive_holds_leases_behind_a_long_flock(self):
        r, s, stolen = self._held_lock_run(steal=True)
        self.assertEqual((s['status'], s['accepted']), ('done', ['a', 'b']))
        self.assertEqual(stolen, {'code': 'bad_state'})  # b's lease was live for the stranger the whole time
        hb = self.conn.execute("SELECT count(*) FROM events WHERE task_id=? AND kind='heartbeat'",
                               (self.rt(r, 'b')['task_id'],)).fetchone()[0]
        self.assertGreaterEqual(hb, 1)
        self.assertEqual(self.events(r, 'b', 'lease_rearmed'), [])

    def test_keepalive_is_load_bearing(self):
        # without it b's lease lapses behind the flock and a stranger's reclaim wins the task: the runner is
        # fenced out (not_owner) and the row pauses instead of silently continuing
        with mock.patch.object(run._KeepAlive, 'tick', lambda self, conn: None):
            r, s, stolen = self._held_lock_run(steal=True)
        self.assertIn('epoch', stolen)
        self.assertEqual((s['status'], s['accepted'], s['paused']), ('paused', ['a'], [{'key': 'b', 'last_error': 'not_owner'}]))
        self.assertEqual(self.task_of(r, 'b').owner, 'stranger')
        self.assertEqual(self.canonical('src/b.py'), 'VALUE = 5\n')

    def test_runner_rearms_its_own_lapsed_lease(self):
        # the lease lapsed behind the flock but nobody reclaimed: the loop renews it under runner.lock and
        # finishes instead of pausing lease_expired (addendum B6: --resume/verify longer than the lease finish)
        with mock.patch.object(run._KeepAlive, 'tick', lambda self, conn: None):
            r, s, _ = self._held_lock_run()
        self.assertEqual((s['status'], s['accepted']), ('done', ['a', 'b']))
        self.assertEqual(self.events(r, 'b', 'lease_rearmed')[0]['epoch'], self.task_of(r, 'b').epoch)
        self.assertEqual(self.canonical('src/b.py'), FOUR)


class ConflictTests(RunBase):
    def pair(self, b_attempts, b_write=('src/b.py',), **top):
        self.ctl(a={'attempts': [{'write': {'src/a.py': SIX}}]}, b={'attempts': b_attempts})
        return plan({'fake': self.fake()}, [task('a', ['src/a.py']), task('b', list(b_write))], **top)

    def test_fail_together_is_rebased_and_redispatched(self):
        r, s = self.run_plan(self.pair([{'write': {'src/b.py': SIX}, 'sleep': 0.3}, {'write': {'src/b.py': FOUR}}]))
        self.assertEqual((s['status'], s['accepted']), ('done', ['a', 'b']))
        self.assertEqual((self.canonical('src/a.py'), self.canonical('src/b.py')), (SIX, FOUR))
        self.assertEqual(self.rt(r, 'b')['rebases'], 1)
        self.assertEqual(self.kinds(r, 'b'), ['dispatched', 'builder_exited', 'submitted', 'verify', 'rebased',
                                              'redispatched', 'builder_exited', 'submitted', 'verify', 'published'])
        rebased = self.events(r, 'b', 'rebased')[0]
        self.assertTrue(os.path.isdir(rebased['retired_worktree']))
        self.assertNotEqual(rebased['worktree'], rebased['retired_worktree'])
        self.assertEqual((Path(rebased['retired_worktree']) / 'src/b.py').read_text(), SIX)  # nothing deleted
        self.assertIn('re-dispatched after a rebase', self.brief(r, 'b', 2))
        self.assertIn('python3 tests/check.py', self.brief(r, 'b', 2))
        self.assertNotIn('a+b =', self.brief(r, 'b', 2))
        self.assert_nothing_leaked(r, 'a+b =')  # the whole database, verify_failed event details included
        self.assertTrue(self.conn.execute("SELECT 1 FROM events WHERE kind='verify_failed'").fetchone())

    def test_failed_rebase_provisioning_keeps_the_delta_for_the_retry(self):
        # git fails during the rebase's re-provision (after step 0 retired the delta-holding worktree): the run
        # pauses, and the retry's claim still transfers from that retired worktree and records the obligation
        p = self.pair([{'write': {'src/b.py': SIX}, 'canonical_write': {'src/b.py': HUMAN}, 'sleep': 0.3},
                       {'write': {'src/b.py': FOUR}}])
        real, hits = gitx.provision_worktree, []

        def flaky(root, dest, links):
            if str(dest).endswith('-e3') and not hits:
                hits.append(str(dest))
                raise gitx.GitError('injected: git worktree add failed')
            return real(root, dest, links)

        with mock.patch.object(gitx, 'provision_worktree', flaky):
            r, s = self.run_plan(p)
        self.assertEqual((s['status'], s['accepted'], s['paused']), ('paused', ['a'], [{'key': 'b', 'last_error': 'GitError'}]))
        tb = self.task_of(r, 'b')
        self.assertEqual((tb.state, tb.worktree_path), ('ready', None))
        retired = json.loads(self.conn.execute("SELECT detail_json FROM events WHERE task_id=? AND kind='retired'",
                                               (tb.id,)).fetchone()[0])['worktree']
        self.assertEqual((Path(retired) / 'src/b.py').read_text(), SIX)
        r2, s2 = self.resume(r.run_id, retry=['b'])
        self.assertEqual((s2['status'], s2['accepted'], self.canonical('src/b.py')), ('done', ['a', 'b'], FOUR))
        self.assertEqual([(o['path'], o['status'], o['retired_worktree']) for o in self.obligations(r2, 'b')],
                         [('src/b.py', 'resolved', retired)])
        b2 = self.brief(r2, 'b', 2)
        self.assertIn('Rebase obligations', b2)
        self.assertIn(retired, b2)
        self.assertEqual((Path(retired) / 'src/b.py').read_text(), SIX)  # still there, nothing deleted

    def test_moved_baseline_records_an_obligation_that_the_builder_resolves(self):
        r, s = self.run_plan(self.pair([{'write': {'src/b.py': SIX}, 'canonical_write': {'src/b.py': HUMAN}, 'sleep': 0.3},
                                        {'write': {'src/b.py': FOUR}}]))
        self.assertEqual((s['status'], s['accepted']), ('done', ['a', 'b']))
        self.assertEqual((self.canonical('src/a.py'), self.canonical('src/b.py')), (SIX, FOUR))
        self.assertEqual(self.rt(r, 'b')['rebases'], 1)
        ob = self.obligations(r, 'b')
        self.assertEqual([(o['path'], o['status']) for o in ob], [('src/b.py', 'resolved')])
        self.assertEqual(self.events(r, 'b', 'rebased')[0]['obligations'], ['src/b.py'])
        b2 = self.brief(r, 'b', 2)
        self.assertIn('Rebase obligations', b2)
        self.assertIn('src/b.py', b2)
        self.assertIn(ob[0]['retired_worktree'], b2)
        self.assertEqual((Path(ob[0]['retired_worktree']) / 'src/b.py').read_text(), SIX)

    def test_ignored_obligation_refuses_submit_then_pauses(self):
        r, s = self.run_plan(self.pair(
            [{'write': {'src/b.py': SIX, 'docs/b.md': 'b\n'}, 'canonical_write': {'src/b.py': HUMAN}, 'sleep': 0.3},
             {'write': {'docs/b.md': 'b2\n'}}], b_write=('src/b.py', 'docs/**'), max_attempts=3))
        self.assertEqual((s['status'], s['accepted'], s['paused']), ('paused', ['a'], [{'key': 'b', 'last_error': 'builder_failed'}]))
        self.assertEqual(self.canonical('src/b.py'), HUMAN)
        self.assertFalse((self.repo / 'docs/b.md').exists())
        reasons = [e['reason'] for e in self.events(r, 'b', 'redispatched')]
        self.assertEqual(reasons, ['rebase', 'rebase_pending'])
        self.assertEqual(self.rt(r, 'b')['attempts'], 3)
        self.assertEqual([o['status'] for o in self.obligations(r, 'b')], ['pending'])
        self.assertIn('src/b.py', self.brief(r, 'b', 3))
        self.assertTrue(os.path.isdir(self.task_of(r, 'b').worktree_path))
        self.assertEqual(self.task_of(r, 'b').state, 'running')

    def test_keep_canonical_marker_goes_through_submit_pending(self):
        p = self.pair([{'write': {'src/b.py': SIX, 'docs/b.md': 'b\n'}, 'canonical_write': {'src/b.py': HUMAN}, 'sleep': 0.3},
                       {'print': ['KEEP-CANONICAL: src/b.py']}], b_write=('src/b.py', 'docs/**'))
        r = self.start(p)
        s, seen = self.step_until_done(r)
        self.assertEqual((s['status'], s['accepted']), ('done', ['a', 'b']))
        self.assertIn('submit_pending', [st['b'] for st in seen])
        self.assertEqual((self.canonical('src/b.py'), self.canonical('docs/b.md')), (HUMAN, 'b\n'))
        self.assertEqual([o['status'] for o in self.obligations(r, 'b')], ['kept'])
        kinds = self.kinds(r, 'b')
        self.assertLess(kinds.index('keep_canonical'), kinds.index('submitted', kinds.index('rebased')))
        self.assertEqual(self.events(r, 'b', 'keep_canonical')[0]['acked'], ['src/b.py'])


class PauseTests(RunBase):
    def test_builder_failed_pauses_and_retry_finishes(self):
        self.ctl(a={'attempts': [{'write': {'src/a.py': SIX}}]}, b={'attempts': [{'exit': 1}]})
        p = plan({'fake': self.fake()}, [task('a', ['src/a.py']), task('b', ['src/b.py'])])
        r, s = self.run_plan(p)
        self.assertEqual((s['status'], s['accepted'], s['paused']), ('paused', ['a'], [{'key': 'b', 'last_error': 'builder_failed'}]))
        self.assertEqual(self.rt(r, 'b')['attempts'], 2)
        self.assertEqual(self.conn.execute('SELECT status FROM runs WHERE id=?', (r.run_id,)).fetchone()[0], 'paused')
        wt = self.task_of(r, 'b').worktree_path
        self.assertTrue(os.path.isdir(wt))
        self.assertIn('builder-b-1.log', self.brief(r, 'b', 2))
        self.assertEqual([a['exit_code'] for a in self.attempts(r, 'b')], [1, 1])
        self.ctl(b={'attempts': [{'write': {'src/b.py': FOUR}}]})
        r2, s2 = self.resume(r.run_id, retry=['b'])
        self.assertEqual((s2['status'], s2['accepted']), ('done', ['a', 'b']))
        self.assertEqual(self.canonical('src/b.py'), FOUR)
        self.assertEqual(self.rt(r2, 'b')['attempts'], 1)  # counters reset by --retry
        self.assertEqual(self.task_of(r2, 'b').worktree_path, wt)  # live lease: same worktree re-dispatched
        self.assertEqual(self.events(r2, None, 'resumed')[0]['retry'], ['b'])

    def test_valid_edit_then_exit_1_is_not_submitted(self):
        self.ctl(a={'attempts': [{'write': {'src/a.py': SIX}, 'exit': 1}]})
        r, s = self.run_plan(plan({'fake': self.fake()}, [task('a', ['src/a.py'])], max_attempts=1))
        self.assertEqual(s['paused'], [{'key': 'a', 'last_error': 'builder_failed'}])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM submissions').fetchone()[0], 0)
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 5\n')
        self.assertNotIn('submitted', self.kinds(r, 'a'))

    def test_pause_while_live_keeps_the_run_alive_and_retry_adopts(self):
        # a non-protocol failure while the attempt is alive pauses the row (catch-all) but the run must not exit
        # with a builder alive, and --retry must adopt that attempt rather than dispatch a second one
        self.ctl(a={'sleep': 1.5, 'write': {'src/a.py': SIX}})
        p = plan({'fake': self.fake('slow_edit.py')}, [task('a', ['src/a.py'])])

        def boom(self_, key, task_):
            raise RuntimeError('injected while the attempt is live')

        with mock.patch.object(run.Runner, '_approve_live_requests', boom):
            r, s = self.run_plan(p, max_iterations=2)
        self.addCleanup(lambda: [x.wait(timeout=10) for x in r._procs.values()])
        self.assertEqual((self.rt(r, 'a')['status'], self.rt(r, 'a')['last_error']), ('paused', 'RuntimeError'))
        self.assertTrue(r._attempt_live('a', 1))
        self.assertEqual(s['status'], 'running')  # paused row, live builder: not "paused", not exit 3
        r2, s2 = self.resume(r.run_id, retry=['a'])
        self.assertEqual((s2['status'], self.canonical('src/a.py'), len(self.marks_for('a'))), ('done', SIX, 1))
        self.assertEqual([a['attempt'] for a in self.attempts(r2, 'a')], [1])

    def test_cancel_while_live_kills_the_builder_and_pauses(self):
        self.ctl(a={'sleep': 30, 'write': {'src/a.py': SIX}})
        r, s = self.run_plan(plan({'fake': self.fake('slow_edit.py')}, [task('a', ['src/a.py'])]), max_iterations=1)
        self.addCleanup(lambda: [x.wait(timeout=10) for x in r._procs.values()])
        self.assertTrue(r._attempt_live('a', 1))
        tasks.cancel(self.conn, self.rt(r, 'a')['task_id'])
        r.max_iterations = None
        s = r.loop()
        self.assertEqual((s['status'], s['paused']), ('paused', [{'key': 'a', 'last_error': 'cancelled'}]))
        self.assertFalse(r._attempt_live('a', 1))
        self.assertEqual(self.events(r, 'a', 'builder_killed')[0]['reason'], 'cancelled')
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 5\n')

    def test_retry_refusals(self):
        self.ctl(a={'attempts': [{'exit': 1}]})
        r, s = self.run_plan(plan({'fake': self.fake()}, [task('a', ['src/a.py'])], max_attempts=1))
        self.assertEqual(s['status'], 'paused')
        for keys in (['zz'], ['a', 'zz']):
            rr = run.Runner(self.conn, self.repo, run_id=r.run_id, quiet=True)
            self.refuse('usage', rr.resume, keys)
        self.refuse('not_found', run.Runner(self.conn, self.repo, run_id='r-nope', quiet=True).resume)


class ScopeTests(RunBase):
    def test_out_of_scope_non_overlapping_is_auto_approved(self):
        self.ctl(a={'attempts': [{'write': {'src/a.py': SIX, 'docs/x.md': 'x\n'}}]})
        r, s = self.run_plan(plan({'fake': self.fake()}, [task('a', ['src/a.py'])], auto_approve_scope=True))
        self.assertEqual(s['status'], 'done')
        self.assertEqual(self.canonical('docs/x.md'), 'x\n')
        self.assertEqual(self.events(r, 'a', 'scope_approved')[0]['path'], 'docs/x.md')
        self.assertEqual(self.conn.execute("SELECT status FROM scope_requests").fetchone()[0], 'approved')
        self.assertEqual(model.load_scopes(self.conn, self.rt(r, 'a')['task_id'])['path'], ['docs/x.md', 'src/a.py'])

    def test_auto_approve_off_pauses_scope_pending(self):
        # Default auto_approve_scope is False: omitting it must pause on out-of-scope edits.
        self.ctl(a={'attempts': [{'write': {'src/a.py': SIX, 'docs/x.md': 'x\n'}}]})
        r, s = self.run_plan(plan({'fake': self.fake()}, [task('a', ['src/a.py'])]))
        self.assertEqual(s['paused'], [{'key': 'a', 'last_error': 'scope_pending'}])
        self.assertEqual(self.events(r, 'a', 'scope_pending')[0]['reason'], 'auto_approve_scope off')
        self.assertEqual(tuple(self.conn.execute("SELECT status, pattern FROM scope_requests").fetchone()), ('pending', 'docs/x.md'))
        self.assertFalse((self.repo / 'docs/x.md').exists())
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 5\n')

    def test_overlapping_out_of_scope_pauses(self):
        self.ctl(a={'attempts': [{'write': {'src/a.py': SIX, 'src/b.py': FOUR}}]},
                 b={'attempts': [{'write': {'src/b.py': FOUR}, 'sleep': 1.0}]})
        r, s = self.run_plan(plan({'fake': self.fake()}, [task('a', ['src/a.py']), task('b', ['src/b.py'])],
                                  auto_approve_scope=True))
        self.assertEqual((s['status'], s['accepted'], s['paused']), ('paused', ['b'], [{'key': 'a', 'last_error': 'scope_pending'}]))
        self.assertEqual(self.events(r, 'a', 'scope_pending')[0]['path'], 'src/b.py')

    def test_scope_needed_marker_redispatches_after_approval(self):
        self.ctl(a={'attempts': [{'print': ['SCOPE-NEEDED: docs/x.md']}, {'write': {'src/a.py': SIX, 'docs/x.md': 'x\n'}}]})
        r, s = self.run_plan(plan({'fake': self.fake()}, [task('a', ['src/a.py'])], auto_approve_scope=True))
        self.assertEqual(s['status'], 'done')
        self.assertEqual(self.canonical('docs/x.md'), 'x\n')
        kinds = self.kinds(r, 'a')
        self.assertEqual(kinds, ['dispatched', 'builder_exited', 'scope_approved', 'redispatched', 'builder_exited',
                                 'submitted', 'verify', 'published'])
        self.assertEqual(self.rt(r, 'a')['attempts'], 1)  # a marker re-dispatch is not a failed attempt
        self.assertEqual(len(self.marks_for('a')), 2)
        self.assertEqual(json.loads((r.run_dir / 'attempt-a-1.markers.json').read_text()),
                         [{'marker': 'SCOPE-NEEDED', 'value': 'docs/x.md'}])

    def test_scope_needed_marker_pauses_by_default(self):
        self.ctl(a={'attempts': [{'print': ['SCOPE-NEEDED: docs/x.md']}]})
        r, s = self.run_plan(plan({'fake': self.fake()}, [task('a', ['src/a.py'])]))
        self.assertEqual(s['paused'], [{'key': 'a', 'last_error': 'scope_pending'}])
        self.assertEqual(self.events(r, 'a', 'scope_pending')[0]['reason'], 'auto_approve_scope off')

    def test_live_builder_cli_request_is_approved(self):
        # the builder itself asks through the protocol while alive: approved before it exits
        self.ctl(a={'attempts': [{'write': {'src/a.py': SIX, 'docs/x.md': 'x\n'}, 'sleep': 1.0}]})
        r = self.start(plan({'fake': self.fake()}, [task('a', ['src/a.py'])], auto_approve_scope=True))
        r.max_iterations = 1
        r.loop()
        t = self.task_of(r, 'a')
        req = tasks.request_scope(self.conn, t.id, t.owner, t.epoch, 'path', 'docs/x.md', 'builder asked')
        r.max_iterations = None
        s = r.loop()
        self.assertEqual(s['status'], 'done')
        self.assertEqual(self.events(r, 'a', 'scope_approved')[0]['request'], req)


class RunnerDeathTests(RunBase):
    def write_plan(self, p, name='plan.json'):
        path = self.scratch / name
        path.write_text(json.dumps(p))
        return path

    def spawn_runner(self, plan_path, crash_at=None, *extra):
        env = dict(os.environ)
        env['PYTHONPATH'] = str(ROOT) + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        if crash_at:
            env['WCP_RUN_CRASH_AT'] = crash_at
        p = subprocess.Popen([sys.executable, '-m', 'wcp', '--json', '--repo', str(self.repo), 'run', '--quiet',
                              str(plan_path), *extra], cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    _CRASH_IN_PROVISION = '''
import os, sys
from wcp import cli, gitx
real = gitx.provision_worktree
def crash(root, dest, links):
    if str(dest).endswith(os.environ['WCP_TEST_CRASH_DEST']):
        os._exit(70)  # the runner dies between _claim's step 0 and step 5
    return real(root, dest, links)
gitx.provision_worktree = crash
sys.exit(cli.main(sys.argv[1:]))
'''

    def spawn_runner_crashing_in_provision(self, plan_path, dest_suffix):
        env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE='1', WCP_TEST_CRASH_DEST=dest_suffix)
        p = subprocess.Popen([sys.executable, '-c', self._CRASH_IN_PROVISION, '--json', '--repo', str(self.repo), 'run',
                              '--quiet', str(plan_path)], cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def wait_for(self, cond, timeout=15.0, what='condition'):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if cond():
                return
            time.sleep(0.05)
        self.fail('timed out waiting for ' + what)

    def crashed(self, p):
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 70, err)

    def run_id(self):
        row = self.conn.execute('SELECT id FROM runs ORDER BY created_at DESC LIMIT 1').fetchone()
        return row[0] if row else None

    def slow_plan(self, sleep=2.0):
        self.ctl(a={'sleep': sleep, 'write': {'src/a.py': SIX}})
        return self.write_plan(plan({'fake': self.fake('slow_edit.py')}, [task('a', ['src/a.py'])]))

    def test_killed_runner_resume_adopts_the_live_attempt(self):
        p = self.spawn_runner(self.slow_plan())
        self.wait_for(lambda: len(self.marks_for('a')) == 1, what='builder start')
        os.kill(p.pid, signal.SIGKILL)
        p.communicate()
        rid = self.run_id()
        probe = run.Runner(self.conn, self.repo, run_id=rid, quiet=True)
        probe.run_dir = db.run_dir(self.repo, rid)
        self.assertTrue(probe._attempt_live('a', 1))  # the wrapper still holds the inherited lock
        self.assertEqual(self.rt(probe, 'a')['status'], 'building')
        r, s = self.resume(rid)
        self.assertEqual((s['status'], s['accepted']), ('done', ['a']))
        self.assertEqual(len(self.marks_for('a')), 1)  # no second builder in the worktree
        self.assertEqual(self.canonical('src/a.py'), SIX)
        self.assertEqual(self.attempts(r, 'a')[-1]['attempt'], 1)

    def test_crash_after_spawn_is_adopted(self):
        p = self.spawn_runner(self.slow_plan(), 'spawn')
        self.crashed(p)
        r, s = self.resume(self.run_id())
        self.assertEqual((s['status'], self.canonical('src/a.py'), len(self.marks_for('a'))), ('done', SIX, 1))
        self.assertIsNotNone(self.attempts(r, 'a')[0]['exit_code'])

    def test_crash_after_intent_redispatches_once(self):
        p = self.spawn_runner(self.slow_plan(0.0), 'intent')
        self.crashed(p)
        rid = self.run_id()
        att = self.conn.execute('SELECT pgid FROM run_attempts WHERE run_id=?', (rid,)).fetchall()
        self.assertEqual([a['pgid'] for a in att], [None])
        r, s = self.resume(rid)
        self.assertEqual((s['status'], self.canonical('src/a.py'), len(self.marks_for('a'))), ('done', SIX, 1))
        self.assertTrue(self.events(r, 'a', 'builder_exited')[0].get('never_started'))
        self.assertEqual([a['attempt'] for a in self.attempts(r, 'a')], [1, 2])
        self.assertEqual(self.rt(r, 'a')['attempts'], 1)

    def test_crash_after_submit_resume_verifies(self):
        p = self.spawn_runner(self.slow_plan(0.0), 'submit')
        self.crashed(p)
        rid = self.run_id()
        self.assertEqual(self.task_of(run.Runner(self.conn, self.repo, run_id=rid), 'a').state, 'submitted')
        r, s = self.resume(rid)
        self.assertEqual((s['status'], self.canonical('src/a.py'), len(self.marks_for('a'))), ('done', SIX, 1))
        self.assertEqual(self.kinds(r, 'a'), ['dispatched', 'builder_exited', 'submitted', 'verify', 'published'])

    def test_crash_after_verify_reservation_and_during_publish(self):
        p = self.spawn_runner(self.slow_plan(0.0), 'verify')
        self.crashed(p)
        rid = self.run_id()
        probe = run.Runner(self.conn, self.repo, run_id=rid)
        self.assertEqual(self.rt(probe, 'a')['status'], 'verifying')
        t = self.task_of(probe, 'a')
        self.assertEqual(t.state, 'submitted')
        # simulate an integrator that died after reserving and then mid-publish (base-design recovery)
        with db.write_txn(self.conn):
            t = model.transition(self.conn, model.load_task(self.conn, t.id), 'verifying')
            model.transition(self.conn, t, 'publishing')
        r, s = self.resume(rid)
        self.assertEqual((s['status'], self.canonical('src/a.py')), ('done', SIX))
        self.assertTrue(self.conn.execute("SELECT 1 FROM events WHERE task_id=? AND kind='publish_finished'",
                                          (t.id,)).fetchone())

    def test_partial_publish_pauses_and_is_never_retried(self):
        p = self.spawn_runner(self.slow_plan(0.0), 'verify')
        self.crashed(p)
        rid = self.run_id()
        t = self.task_of(run.Runner(self.conn, self.repo, run_id=rid), 'a')
        with db.write_txn(self.conn):
            t = model.transition(self.conn, model.load_task(self.conn, t.id), 'verifying')
            model.transition(self.conn, t, 'publishing')
        write(self.repo, 'src/a.py', 'VALUE = 9\n')  # neither before nor after: a third state
        r, s = self.resume(rid)
        self.assertEqual((s['status'], s['paused']), ('paused', [{'key': 'a', 'last_error': 'partial_publish'}]))
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 9\n')
        self.refuse('usage', run.Runner(self.conn, self.repo, run_id=rid, quiet=True).resume, ['a'])
        self.refuse('usage', run.Runner(self.conn, self.repo, run_id=rid, quiet=True).resume, (), True)

    def test_partial_publish_quarantine_is_never_rebased_on_resume(self):
        # the verdict landed without the runner recording it (a human ran `wcp task verify` by hand and it
        # quarantined): a verifying row over a conflicted/partial_publish task pauses, never rebases
        p = self.spawn_runner(self.slow_plan(0.0), 'verify')
        self.crashed(p)
        rid = self.run_id()
        t = self.task_of(run.Runner(self.conn, self.repo, run_id=rid), 'a')
        with db.write_txn(self.conn):
            t = model.transition(self.conn, model.load_task(self.conn, t.id), 'verifying')
            t = model.transition(self.conn, t, 'publishing')
            t = model.transition(self.conn, t, 'conflicted', blocked_reason='partial_publish: src/a.py')
        r, s = self.resume(rid)
        self.assertEqual((s['status'], s['paused']), ('paused', [{'key': 'a', 'last_error': 'partial_publish'}]))
        self.assertEqual(self.kinds(r, 'a'), ['dispatched', 'builder_exited', 'submitted', 'verify', 'paused'])  # no rebase, no reclaim
        self.assertEqual(self.events(r, 'a', 'paused')[0]['paths'], ['src/a.py'])
        t2 = self.task_of(r, 'a')
        self.assertEqual((t2.state, t2.epoch, t2.worktree_path, t2.blocked_reason),
                         ('conflicted', t.epoch, t.worktree_path, 'partial_publish: src/a.py'))
        with db.write_txn(self.conn):  # even with another code recorded on the row, the task's quarantine refuses --retry
            self.conn.execute("UPDATE run_tasks SET last_error='bad_state' WHERE run_id=? AND key='a'", (rid,))
        self.refuse('usage', run.Runner(self.conn, self.repo, run_id=rid, quiet=True).resume, ['a'])

    def test_crash_during_rebase_provisioning_is_reclaimed_with_the_delta(self):
        # the runner dies inside the rebase's re-provision (row verifying, task provisioning, the delta in the
        # retired worktree): --resume reclaims, transfers, records the obligation, and the work reaches canonical
        self.ctl(a={'attempts': [{'write': {'src/a.py': SIX}}]},
                 b={'attempts': [{'write': {'src/b.py': SIX}, 'canonical_write': {'src/b.py': HUMAN}, 'sleep': 0.3},
                                 {'write': {'src/b.py': FOUR}}]})
        pp = self.write_plan(plan({'fake': self.fake()}, [task('a', ['src/a.py']), task('b', ['src/b.py'])]))
        self.crashed(self.spawn_runner_crashing_in_provision(pp, '-e3'))
        rid = self.run_id()
        probe = run.Runner(self.conn, self.repo, run_id=rid)
        tb = self.task_of(probe, 'b')
        self.assertEqual((self.rt(probe, 'b')['status'], tb.state, tb.epoch), ('verifying', 'provisioning', 3))
        retired = [w for w in model.load_worktrees(self.conn, tb.id) if w.status == 'retired']
        self.assertEqual([w.epoch for w in retired], [2])
        self.assertEqual((Path(retired[0].path) / 'src/b.py').read_text(), SIX)
        r, s = self.resume(rid)
        self.assertEqual((s['status'], s['accepted'], s['paused']), ('done', ['a', 'b'], []))
        self.assertEqual((self.canonical('src/a.py'), self.canonical('src/b.py')), (SIX, FOUR))
        rc = self.events(r, 'b', 'reclaimed')[0]
        self.assertEqual((rc['epoch'], rc['retired_worktree'], rc['obligations']), (4, retired[0].path, ['src/b.py']))
        self.assertEqual([(o['epoch'], o['path'], o['status']) for o in self.obligations(r, 'b')], [(4, 'src/b.py', 'resolved')])
        b2 = self.brief(r, 'b', 2)
        self.assertIn('Rebase obligations', b2)
        self.assertIn(retired[0].path, b2)
        self.assertEqual((Path(retired[0].path) / 'src/b.py').read_text(), SIX)
        self.assertEqual(len(self.marks_for('b')), 2)

    def test_building_row_with_provisioning_task_is_reclaimed(self):
        p = self.spawn_runner(self.slow_plan(0.0), 'claim')
        self.crashed(p)
        rid = self.run_id()
        t = self.task_of(run.Runner(self.conn, self.repo, run_id=rid), 'a')
        with db.write_txn(self.conn):  # died mid-claim after the row already said building (B6 row 7)
            self.conn.execute("UPDATE tasks SET state='provisioning' WHERE id=?", (t.id,))
            self.conn.execute("UPDATE worktrees SET status='provisioning' WHERE path=?", (t.worktree_path,))
            self.conn.execute("UPDATE run_tasks SET status='building' WHERE run_id=? AND key='a'", (rid,))
        r, s = self.resume(rid)
        self.assertEqual((s['status'], s['paused'], self.canonical('src/a.py'), len(self.marks_for('a'))), ('done', [], SIX, 1))
        self.assertEqual(self.events(r, 'a', 'reclaimed')[0]['epoch'], 2)

    def test_resume_after_the_lease_lapsed_finishes_without_rerunning_the_builder(self):
        self.ctl(a={'sleep': 0.0, 'write': {'src/a.py': SIX}})
        pp = self.write_plan(plan({'fake': self.fake('slow_edit.py')}, [task('a', ['src/a.py'])], lease_s=2))
        p = self.spawn_runner(pp, 'spawn')
        self.crashed(p)
        rid = self.run_id()
        self.wait_for(lambda: (db.run_dir(self.repo, rid) / 'attempt-a-1.exit.json').exists(), what='builder exit')
        self.expire()  # the dead runner's lease is long gone
        self.assertTrue(self.task_of(run.Runner(self.conn, self.repo, run_id=rid), 'a').lease_expired(db.now()))
        r, s = self.resume(rid)
        self.assertEqual((s['status'], s['paused'], self.canonical('src/a.py'), len(self.marks_for('a'))), ('done', [], SIX, 1))
        self.assertEqual(self.events(r, 'a', 'lease_rearmed')[0]['epoch'], 1)
        # the crash at 'spawn' precedes the dispatched event; resume re-arms first, then reaps and submits
        self.assertEqual(self.kinds(r, 'a'), ['lease_rearmed', 'builder_exited', 'submitted', 'verify', 'published'])

    def test_crash_after_claim_and_mid_provisioning(self):
        p = self.spawn_runner(self.slow_plan(0.0), 'claim')
        self.crashed(p)
        rid = self.run_id()
        probe = run.Runner(self.conn, self.repo, run_id=rid)
        t = self.task_of(probe, 'a')
        self.assertEqual((self.rt(probe, 'a')['status'], t.state, t.epoch), ('pending', 'claimed', 1))
        # rewind the durable record to "died mid-claim": task and worktree row both provisioning, lease live
        with db.write_txn(self.conn):
            self.conn.execute("UPDATE tasks SET state='provisioning' WHERE id=?", (t.id,))
            self.conn.execute("UPDATE worktrees SET status='provisioning' WHERE path=?", (t.worktree_path,))
        r, s = self.resume(rid)
        self.assertEqual((s['status'], self.canonical('src/a.py'), len(self.marks_for('a'))), ('done', SIX, 1))
        self.assertEqual(self.task_of(r, 'a').epoch, 2)  # runner_reclaim on the run's own live lease
        self.assertEqual(self.events(r, 'a', 'reclaimed')[0]['epoch'], 2)

    def test_crash_after_claim_redispatches_in_place(self):
        p = self.spawn_runner(self.slow_plan(0.0), 'claim')
        self.crashed(p)
        r, s = self.resume(self.run_id())
        self.assertEqual((s['status'], self.canonical('src/a.py'), self.task_of(r, 'a').epoch), ('done', SIX, 1))

    def test_max_iterations_leaves_building_then_resume_completes(self):
        self.ctl(a={'sleep': 1.0, 'write': {'src/a.py': SIX}})
        p = plan({'fake': self.fake('slow_edit.py')}, [task('a', ['src/a.py'])])
        r, s = self.run_plan(p, max_iterations=1)
        self.addCleanup(lambda: [x.wait(timeout=10) for x in r._procs.values()])
        self.assertEqual((s['status'], self.rt(r, 'a')['status']), ('running', 'building'))
        r2, s2 = self.resume(r.run_id)
        self.assertEqual((s2['status'], len(self.marks_for('a'))), ('done', 1))

    def test_resume_while_initial_runner_alive_refuses_lock_held(self):
        p = self.spawn_runner(self.slow_plan(3.0))
        self.wait_for(lambda: len(self.marks_for('a')) == 1, what='builder start')
        rid = self.run_id()
        self.refuse('lock_held', run.Runner(self.conn, self.repo, run_id=rid, quiet=True).resume)
        out, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])['status'], 'done')

    def test_cli_exit_codes_and_json_shape(self):
        self.ctl(a={'attempts': [{'write': {'src/a.py': SIX}}]}, b={'attempts': [{'exit': 1}]})
        good = self.write_plan(plan({'fake': self.fake()}, [task('a', ['src/a.py'])]), 'good.json')
        rc, out = self.cli('run', '--quiet', good)
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(out), ['accepted', 'ok', 'paused', 'run_id', 'status'])
        self.assertEqual((out['ok'], out['status'], out['accepted'], out['paused']), (True, 'done', ['a'], []))
        bad = self.write_plan(plan({'fake': self.fake()}, [task('b', ['src/b.py'])], max_attempts=1), 'bad.json')
        rc, out = self.cli('run', '--quiet', bad)
        self.assertEqual((rc, out['status'], out['paused']), (3, 'paused', [{'key': 'b', 'last_error': 'builder_failed'}]))
        rc, out = self.cli('run', '--resume', out['run_id'], '--retry', 'b', '--quiet')
        self.assertEqual((rc, out['status']), (3, 'paused'))
        invalid = self.write_plan({'version': 1, 'builders': {}, 'tasks': []}, 'invalid.json')
        self.assertEqual(self.cli('run', invalid)[0], 2)
        self.assertEqual(self.cli('run', good, '--retry', 'a')[0], 2)
        self.assertEqual(self.cli('run')[0], 2)
        self.assertEqual(self.cli('run', '--resume', 'r-000000')[0], 3)
        self.ctl(a={'attempts': [{'write': {'src/a.py': SEVEN}}]})  # canonical is 6 by now: a fresh edit
        p = subprocess.run([sys.executable, '-m', 'wcp', '--repo', str(self.repo), 'run', str(good)], cwd=str(ROOT),
                           env=dict(os.environ, PYTHONPATH=str(ROOT) + os.pathsep + os.environ.get('PYTHONPATH', '')),
                           capture_output=True, text=True, timeout=120)
        self.assertIn('run_id:', p.stdout, p.stderr)  # human output: status lines, then the summary


class SafetyTests(RunBase):
    def test_builder_env_is_sanitized(self):
        os.environ['WCP_ROLE'] = 'integrator'
        os.environ['WCP_FOO'] = 'leak'
        leaked_vars = {'AWS_SECRET_ACCESS_KEY': 'leak', 'GITHUB_TOKEN': 'leak', 'GIT_SSH_COMMAND': 'leak'}
        for k, v in leaked_vars.items():
            old = os.environ.get(k)
            os.environ[k] = v
            self.addCleanup(lambda k_=k, old_=old: os.environ.pop(k_, None) if old_ is None else os.environ.__setitem__(k_, old_))
        out = self.scratch / 'env.json'
        self.ctl(a={'out': str(out), 'write': {'src/a.py': SIX}})
        r, s = self.run_plan(plan({'fake': self.fake('env_dump.py', extra=['{brief}', '{key}', '{task}', '{epoch}'])},
                                  [task('a', ['src/a.py'])]))
        self.assertEqual(s['status'], 'done', s)
        seen = json.loads(out.read_text())
        self.assertEqual(set(seen['env']), set(run.ENV_VARS))
        self.assertTrue(set(seen['all_keys']).isdisjoint(leaked_vars))
        self.assertLessEqual(set(seen['all_keys']) - {'__CF_USER_TEXT_ENCODING', 'LC_CTYPE'},
                             set(run.BUILDER_ENV_ALLOWLIST) | set(run.ENV_VARS))
        self.assertEqual(seen['env']['WCP_ROLE'], 'builder')
        self.assertEqual(seen['env']['WCP_HOME'], str(self.home))
        t = self.task_of(r, 'a')
        self.assertEqual(seen['argv'][-3:], ['a', t.id, str(t.epoch)])
        self.assertIn('# wcp builder brief', seen['argv'][-4])
        self.assertEqual(os.path.realpath(seen['cwd']), os.path.realpath(t.worktree_path))

    def test_secret_sentinels_are_redacted_and_never_leave_the_log(self):
        self.ctl(a={'attempts': [{'write': {'src/a.py': SIX},
                                  'print': [SK + ' leaked', 'header ' + PEM + ' tail', 'token=abcdefghijkl', 'plain line',
                                            'KEEP-CANONICAL: ' + SK, 'SCOPE-NEEDED: ' + PEM]}]})
        r, s = self.run_plan(plan({'fake': self.fake()}, [task('a', ['src/a.py'])]))
        self.assertEqual(s['status'], 'done')
        log = self.log(r, 'a', 1).decode()
        self.assertEqual(log.count('<redacted:'), 5)
        self.assertIn('plain line', log)
        for needle in (SK, PEM, 'abcdefghijkl'):
            self.assertNotIn(needle, log)
        self.assert_nothing_leaked(r, SK, PEM, 'abcdefghijkl', 'plain line')
        markers = r.run_dir / 'attempt-a-1.markers.json'
        self.assertFalse(markers.exists(), 'a secret-shaped marker line is redacted before it can be a marker')
        self.assertEqual(self.events(r, 'a', 'keep_canonical') + self.events(r, 'a', 'scope_approved'), [])

    def test_wrapper_caps_and_dedupes_markers_after_redaction(self):
        rd = self.scratch / 'rd'
        rd.mkdir()
        fd = os.open(str(rd / 'attempt-k-1.lock'), os.O_RDWR | os.O_CREAT, 0o600)
        code = ('print("KEEP-CANONICAL: " + "y" * 5000)\n'
                'for i in range(70): print("SCOPE-NEEDED: p%d" % i)\n'
                'print("SCOPE-NEEDED: p0")\n'
                'print("KEEP-CANONICAL: ' + SK + '")\n')
        p = subprocess.run([sys.executable, '-I', run._WRAPPER, str(rd), 'k', '1', str(fd), '--', sys.executable, '-c', code],
                           pass_fds=(fd,), stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        os.close(fd)
        self.assertEqual(p.returncode, 0, p.stderr)
        text = (rd / 'attempt-k-1.markers.json').read_text()
        markers = json.loads(text)
        self.assertEqual(len(markers), 64)
        self.assertEqual(len({(m['marker'], m['value']) for m in markers}), 64)
        self.assertEqual((markers[0]['marker'], len(markers[0]['value'])), ('KEEP-CANONICAL', 4096))
        self.assertEqual(markers[-1], {'marker': 'SCOPE-NEEDED', 'value': 'p62'})
        self.assertNotIn(SK, text)
        self.assertIn(b'<redacted:', (rd / 'builder-k-1.log').read_bytes())

    def test_wrapper_is_not_shadowed_by_a_wcp_package_in_the_worktree(self):
        # this repository has a top-level wcp/ package: a builder's worktree copy of it (possibly broken by an
        # earlier attempt) must never be what the runner executes as the lock holder
        write(self.repo, 'wcp/__init__.py', '')
        write(self.repo, 'wcp/_attempt.py', 'raise SystemExit(99)\n')
        commit(self.repo, 'shadow wrapper')
        self.ctl(a={'attempts': [{'write': {'src/a.py': SIX}}]})
        r, s = self.run_plan(plan({'fake': self.fake()}, [task('a', ['src/a.py'])]))
        self.assertEqual((s['status'], s['paused'], self.canonical('src/a.py'), len(self.marks_for('a'))), ('done', [], SIX, 1))
        self.assertEqual([a['exit_code'] for a in self.attempts(r, 'a')], [0])
        self.assertTrue((Path(self.task_of(r, 'a').worktree_path) / 'wcp/_attempt.py').exists())

    def test_never_started_twice_pauses_builder_lost(self):
        self.ctl(a={'attempts': [{'write': {'src/a.py': SIX}}]})
        with mock.patch.object(run.Runner, '_spawn', lambda self_, key, n, argv, ctx: 4242):  # nothing ever starts
            r, s = self.run_plan(plan({'fake': self.fake()}, [task('a', ['src/a.py'])]))
        self.assertEqual(s['paused'], [{'key': 'a', 'last_error': 'builder_lost'}])
        self.assertEqual([a['attempt'] for a in self.attempts(r, 'a')], [1, 2])  # bounded, not one row per poll
        self.assertTrue(all(a['exited_at'] for a in self.attempts(r, 'a')))
        self.assertEqual([e.get('never_started') for e in self.events(r, 'a', 'builder_exited')], [True, True])
        self.assertEqual(self.rt(r, 'a')['attempts'], 1)

    def test_big_output_is_capped_while_running_and_markers_survive(self):
        out = self.scratch / 'size.txt'
        self.ctl(a={'mib': 2, 'marker': 'KEEP-CANONICAL: nothing', 'out': str(out), 'write': {'src/a.py': SIX}})
        r, s = self.run_plan(plan({'fake': self.fake('big_output.py', timeout_s=120)}, [task('a', ['src/a.py'])]))
        self.assertEqual(s['status'], 'done', s)
        self.assertLessEqual(int(out.read_text()), 1024 * 1024)
        log = self.log(r, 'a', 1)
        self.assertLessEqual(len(log), 1024 * 1024)
        self.assertIn(b'[wcp: ', log)
        self.assertIn(b'<redacted:', log)
        self.assertNotIn(b'0123456789abcdef', log)
        self.assertEqual(json.loads((r.run_dir / 'attempt-a-1.markers.json').read_text()),
                         [{'marker': 'KEEP-CANONICAL', 'value': 'nothing'}])
        self.assertEqual(self.events(r, 'a', 'keep_canonical')[0]['unknown'], ['nothing'])

    def test_timeout_kills_the_whole_group(self):
        pidfile = self.scratch / 'grandchild.pid'
        self.ctl(a={'pidfile': str(pidfile)})
        r, s = self.run_plan(plan({'fake': self.fake('sleeper.py', timeout_s=1)}, [task('a', ['src/a.py'])], max_attempts=1))
        self.assertEqual(s['paused'], [{'key': 'a', 'last_error': 'builder_failed'}])
        self.assertEqual(self.events(r, 'a', 'builder_killed')[0]['reason'], 'timeout')
        self.assertEqual(self.attempts(r, 'a')[0]['exit_code'], -signal.SIGTERM)
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(pid, signal.SIGKILL)
            self.fail('grandchild sleeper survived the group kill')

    def test_catch_all_pauses_on_unsupported_path(self):
        self.ctl(a={'attempts': [{'write': {'src/a.py': SIX}, 'symlink': {'src/link.py': 'a.py'}}]})
        r, s = self.run_plan(plan({'fake': self.fake()}, [task('a', ['src/a.py'])]))
        self.assertEqual(s['paused'], [{'key': 'a', 'last_error': 'unsupported_path'}])
        t = self.task_of(r, 'a')
        self.assertIn(t.state, ('claimed', 'running'))  # left exactly where the refusal found it
        self.assertTrue(os.path.islink(Path(t.worktree_path) / 'src/link.py'))
        self.assertEqual((Path(t.worktree_path) / 'src/a.py').read_text(), SIX)
        self.assertEqual(self.canonical('src/a.py'), 'VALUE = 5\n')

    def test_failed_integrated_check_brief_has_command_and_status_but_no_output(self):
        self.ctl(a={'attempts': [{'write': {'src/a.py': SEVEN}}, {'write': {'src/a.py': SIX}}]})
        r, s = self.run_plan(plan({'fake': self.fake()}, [task('a', ['src/a.py'])]))
        self.assertEqual((s['status'], self.canonical('src/a.py')), ('done', SIX))
        self.assertEqual(self.events(r, 'a', 'redispatched')[0]['reason'], 'checks_failed')
        b2 = self.brief(r, 'a', 2)
        self.assertIn('`python3 tests/check.py` exited 1', b2)
        self.assertNotIn('a+b =', b2)
        self.assert_nothing_leaked(r, 'a+b =')
        self.assertEqual(self.rt(r, 'a')['rebases'], 0)


if __name__ == '__main__':
    unittest.main()
