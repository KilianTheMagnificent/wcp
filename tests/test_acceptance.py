"""The ten spec §4.2 acceptance tests, black-box through `python3 -m wcp` (subprocess + WCP_HOME).

Every test gets a fresh WCP_HOME and a fresh fixture repo (README.md, src/a.py, src/b.py = 5,
tests/check.py exits 0 iff a+b <= 11) registered with `wcp init --check "python3 tests/check.py"`.
Nothing here imports wcp; the contract under test is the spec's "CLI surface", exit codes
(0 ok / 2 usage / 3 typed refusal) and the --json shapes.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from fixtures import git_env, make_home, make_repo, write

ROOT = Path(__file__).resolve().parents[1]
SIX = 'VALUE = 6\n'
FIVE = 'VALUE = 5\n'

# Waits until a barrier file exists, then becomes `python3 -m wcp <args>` (same interpreter).
_RACER = '''import os, sys, time
ready, barrier = sys.argv[1], sys.argv[2]
open(ready, "w").close()
while not os.path.exists(barrier):
    time.sleep(0.001)
os.execv(sys.executable, [sys.executable, "-m", "wcp"] + sys.argv[3:])
'''


def sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def tree_bytes(root: Path) -> dict:
    """Every regular file under root except .git, as {relpath: bytes}: the canonical-untouched oracle."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != '.git']
        for f in filenames:
            p = Path(dirpath) / f
            if p.is_file():
                out[str(p.relative_to(root))] = p.read_bytes()
    return out


def _task(obj: dict) -> dict:
    """Task fields: nested under 'task' when the command returns more than the task, else flat."""
    t = obj.get('task')
    return t if isinstance(t, dict) else obj


def _parse(out: str, err: str) -> dict:
    for text in (out, err):
        try:
            return json.loads(text)
        except ValueError:
            continue
    raise AssertionError('no JSON on either stream\nstdout: %s\nstderr: %s' % (out, err))


def _first(obj: dict, *keys):
    for k in keys:
        if k in obj:
            return obj[k]
    raise AssertionError('none of %s in %s' % (keys, json.dumps(obj)[:800]))


def claude_payload(path, cwd, tool='Write'):
    return {'session_id': 's-acc', 'prompt_id': 'p1', 'transcript_path': '/dev/null', 'cwd': str(cwd),
            'permission_mode': 'default', 'hook_event_name': 'PreToolUse', 'tool_name': tool,
            'tool_input': {'file_path': str(path), 'content': 'VALUE = 9\n'}, 'tool_use_id': 'tu1'}


def grok_payload(path, cwd):
    return {'hookEventName': 'pre_tool_use', 'sessionId': 's-acc', 'cwd': str(cwd), 'workspaceRoot': str(cwd),
            'permissionMode': 'default', 'toolName': 'search_replace',
            'toolInput': {'filePath': str(path), 'oldString': 'VALUE = 5', 'newString': 'VALUE = 9'},
            'toolUseId': 'tu1', 'toolInputTruncated': False, 'promptId': 'p1', 'timestamp': 0}


def codex_apply_patch_payload(rel, cwd):
    patch = '*** Begin Patch\n*** Update File: %s\n@@\n-VALUE = 5\n+VALUE = 9\n*** End Patch\n' % rel
    return {'cwd': str(cwd), 'hook_event_name': 'PreToolUse', 'model': 'gpt-5-codex', 'permission_mode': 'default',
            'session_id': 's-acc', 'tool_input': {'patch': patch}, 'tool_name': 'apply_patch',
            'tool_use_id': 'tu1', 'transcript_path': '/dev/null', 'turn_id': 't1'}


class AcceptanceBase(unittest.TestCase):
    def setUp(self):
        self.home = make_home()
        self.addCleanup(shutil.rmtree, str(self.home), True)
        self.repo = make_repo()
        self.addCleanup(shutil.rmtree, str(self.repo), True)
        env = git_env()
        env.pop('WCP_ROLE', None)
        env.update({'WCP_HOME': str(self.home), 'PYTHONPATH': str(ROOT), 'PYTHONDONTWRITEBYTECODE': '1'})
        self.env = env
        p = self.run_wcp(['init', '--check', 'python3 tests/check.py'])
        self.assertEqual(p.returncode, 0, 'wcp init failed: %s%s' % (p.stdout, p.stderr))

    # -- process plumbing -------------------------------------------------------------------
    def run_wcp(self, args, cwd=None, stdin=None):
        return subprocess.run([sys.executable, '-m', 'wcp', *args], cwd=str(cwd or self.repo), env=self.env,
                              input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=120)

    def call(self, *args, cwd=None):
        """Run with --json; return (returncode, parsed object)."""
        p = self.run_wcp([*args, '--json'], cwd)
        try:
            return p.returncode, _parse(p.stdout, p.stderr)
        except AssertionError as e:
            self.fail('`wcp %s` (rc %d): %s' % (' '.join(args), p.returncode, e))

    def ok(self, *args, cwd=None):
        rc, obj = self.call(*args, cwd=cwd)
        self.assertEqual(rc, 0, '`wcp %s` -> rc %d: %s' % (' '.join(args), rc, obj))
        self.assertIs(obj.get('ok'), True, obj)
        return obj

    def refused(self, code, *args, cwd=None):
        """Assert exit 3 with the typed code; return the error object."""
        rc, obj = self.call(*args, cwd=cwd)
        self.assertEqual(rc, 3, '`wcp %s` -> rc %d: %s' % (' '.join(args), rc, obj))
        self.assertIs(obj.get('ok'), False, obj)
        self.assertEqual(obj.get('code'), code, obj)
        return obj

    def hook(self, payload, cwd):
        return self.run_wcp(['hook', 'pretooluse'], cwd=cwd, stdin=json.dumps(payload))

    # -- protocol helpers -------------------------------------------------------------------
    def task(self, *create_args, ready=True):
        obj = self.ok('task', 'create', '--goal', 'acceptance', *create_args)
        tid = obj.get('task_id') or _task(obj).get('id')
        self.assertTrue(tid, obj)
        if ready:
            self.ok('task', 'ready', tid)
        return tid

    def claim(self, tid, owner, lease=None):
        args = ['task', 'claim', tid, '--owner', owner] + (['--lease', str(lease)] if lease else [])
        obj = self.ok(*args)
        wt = obj['worktree']
        wt = Path(wt if isinstance(wt, str) else wt['path'])
        self.assertTrue(wt.is_dir(), 'claim printed a worktree that does not exist: %s' % wt)
        self.assertIn('codex -C', obj.get('codex_hint', ''), obj)
        return wt, int(obj['epoch']), obj

    def submit(self, tid, owner, epoch, *extra):
        return self.ok('task', 'submit', tid, '--owner', owner, '--epoch', str(epoch), *extra)

    def show(self, tid):
        return _task(self.ok('task', 'show', tid))

    def assert_canonical_untouched(self, snapshot):
        self.assertEqual(tree_bytes(self.repo), snapshot, 'canonical checkout changed')


class AcceptanceTests(AcceptanceBase):

    def test_01_two_claims_race(self):
        tid = self.task('--write', 'src/a.py')
        barrier = self.home / 'barrier'
        procs = []
        for i in (1, 2):
            ready = self.home / ('ready-%d' % i)
            p = subprocess.Popen(
                [sys.executable, '-c', _RACER, str(ready), str(barrier),
                 'task', 'claim', tid, '--owner', 'racer-%d' % i, '--json'],
                cwd=str(self.repo), env=self.env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            procs.append((ready, p))
        deadline = time.time() + 30
        while not all(r.exists() for r, _ in procs):
            self.assertLess(time.time(), deadline, 'racers never reached the barrier')
            time.sleep(0.005)
        barrier.touch()  # both racers exec `wcp task claim` now
        results = []
        for i, (_, p) in enumerate(procs, 1):
            out, err = p.communicate(timeout=120)
            results.append(('racer-%d' % i, p.returncode, out, err))
        self.assertEqual(sorted(r[1] for r in results), [0, 3], results)
        winner = [r for r in results if r[1] == 0][0]
        loser = [r for r in results if r[1] == 3][0]
        lobj = _parse(loser[2], loser[3])
        self.assertIs(lobj['ok'], False, lobj)
        # spec: "the other bad_state/lease refusal" -- the task is no longer ready and its lease is live
        self.assertIn(lobj['code'], ('bad_state', 'lease_expired'), lobj)
        wobj = _parse(winner[2], winner[3])
        self.assertEqual(int(wobj['epoch']), 1, wobj)
        t = self.show(tid)
        self.assertEqual(t['state'], 'claimed', t)
        self.assertEqual(t['owner'], winner[0], t)
        self.assertEqual(int(t['epoch']), 1, 'exactly one epoch consumed: %s' % t)
        # the global counter advanced exactly once: the next claim anywhere gets epoch 2
        other = self.task('--write', 'src/b.py')
        _, epoch2, _ = self.claim(other, 'third')
        self.assertEqual(epoch2, 2)

    def test_02_overlap_serialized_at_claim(self):
        """Addendum A1/A2: `ready` reports overlap as waiting_on instead of refusing; the unchanged atomic
        claim is where overlapping tasks serialize (scope_overlap carries waiting_on)."""
        a = self.task('--write', 'src/a.py')
        b = self.task('--write', 'src/**', ready=False)
        obj = self.ok('task', 'ready', b)
        self.assertEqual((obj['state'], obj['waiting_on']), ('ready', []))  # a is ready, not yet active
        self.assertEqual(self.show(b)['state'], 'ready')
        self.claim(a, 'alice')
        err = self.refused('scope_overlap', 'task', 'claim', b, '--owner', 'bob')
        self.assertEqual(err['detail']['waiting_on'], [a])
        self.assertEqual(self.show(b)['state'], 'ready')
        # resources: string equality
        c = self.task('--write', 'docs/c.md', '--resource', 'port:5199')
        d = self.task('--write', 'docs/d.md', '--resource', 'port:5199', ready=False)
        self.ok('task', 'ready', d)
        self.claim(c, 'carol')
        err = self.refused('scope_overlap', 'task', 'claim', d, '--owner', 'dave')
        self.assertEqual(err['detail']['waiting_on'], [c])
        # scope approve that would overlap an active task is refused the same way (unchanged)
        f = self.task('--write', 'tests/**')
        _, ef, _ = self.claim(f, 'frank')
        req = self.ok('scope', 'request', f, '--owner', 'frank', '--epoch', str(ef),
                      '--write', 'src/a.py', '--reason', 'need a.py too')
        rid = _first(req, 'request_id', 'req_id', 'id')
        self.refused('scope_overlap', 'scope', 'approve', f, str(rid))
        # macOS case alias: SRC/A.PY is the same file as src/a.py -- admitted, reported, refused at claim
        g = self.task('--write', 'SRC/A.PY', ready=False)
        obj = self.ok('task', 'ready', g)
        self.assertEqual(obj['waiting_on'], [a])
        self.refused('scope_overlap', 'task', 'claim', g, '--owner', 'gus')

    def test_03_expired_epoch(self):
        tid = self.task('--write', 'src/a.py')
        w1, e1, _ = self.claim(tid, 'old-owner')
        self.assertEqual(e1, 1)
        write(w1, 'src/a.py', SIX)
        sub = self.submit(tid, 'old-owner', e1)  # a clean bundle under epoch 1
        self.assertEqual(sub['state'], 'submitted', sub)
        self.ok('task', 'release', tid)  # coordinator force-expires the lease: new epoch fences the old owner
        released = self.show(tid)
        self.assertGreater(int(released['epoch']), e1, released)
        self.refused('stale_epoch', 'task', 'heartbeat', tid, '--owner', 'old-owner', '--epoch', str(e1))
        self.refused('stale_epoch', 'task', 'submit', tid, '--owner', 'old-owner', '--epoch', str(e1))
        snap = tree_bytes(self.repo)
        self.refused('stale_epoch', 'task', 'verify', tid)  # even a clean bundle cannot verify under the old epoch
        self.assert_canonical_untouched(snap)
        self.assertEqual((self.repo / 'src/a.py').read_text(), FIVE)
        # reclaim by another owner
        w2, e2, _ = self.claim(tid, 'new-owner')
        self.assertGreater(e2, int(released['epoch']))
        self.assertNotEqual(w2, w1)
        self.refused('stale_epoch', 'task', 'heartbeat', tid, '--owner', 'old-owner', '--epoch', str(e1))
        self.refused('stale_epoch', 'task', 'submit', tid, '--owner', 'old-owner', '--epoch', str(e1))
        self.ok('task', 'heartbeat', tid, '--owner', 'new-owner', '--epoch', str(e2))
        self.assert_canonical_untouched(snap)

    def test_04_before_hash_drift(self):
        tid = self.task('--write', 'src/a.py')
        w, e, _ = self.claim(tid, 'builder')
        write(w, 'src/a.py', SIX)
        write(self.repo, 'src/a.py', 'VALUE = 4\n')  # canonical moves under the builder, after the claim
        self.assertEqual(self.submit(tid, 'builder', e)['state'], 'submitted')
        snap = tree_bytes(self.repo)
        err = self.refused('before_hash_drift', 'task', 'verify', tid)
        self.assertIn('src/a.py', json.dumps(err))
        self.assert_canonical_untouched(snap)
        self.assertEqual((self.repo / 'src/a.py').read_text(), 'VALUE = 4\n')
        self.assertIn(self.show(tid)['state'], ('conflicted', 'running'))

    def test_05_out_of_scope_path(self):
        tid = self.task('--write', 'src/a.py')
        w, e, _ = self.claim(tid, 'builder')
        write(w, 'src/b.py', SIX)
        err = self.refused('out_of_scope', 'task', 'submit', tid, '--owner', 'builder', '--epoch', str(e))
        self.assertIn('src/b.py', json.dumps(err))
        self.assertIn(self.show(tid)['state'], ('claimed', 'running'))
        stored = list(self.home.glob('repos/*/bundles/%s/*' % tid))
        self.assertEqual(stored, [], 'a submission was stored despite out_of_scope')
        self.assertEqual((self.repo / 'src/b.py').read_text(), FIVE)

    def test_06_fail_together(self):
        a = self.task('--write', 'src/a.py')
        b = self.task('--write', 'src/b.py')
        wa, ea, _ = self.claim(a, 'alice')
        wb, eb, _ = self.claim(b, 'bob')
        write(wa, 'src/a.py', SIX)  # 6 + 5 = 11: passes alone
        write(wb, 'src/b.py', SIX)  # 5 + 6 = 11: passes alone
        self.assertEqual(self.submit(a, 'alice', ea)['state'], 'submitted')
        self.assertEqual(self.submit(b, 'bob', eb)['state'], 'submitted')
        va = self.ok('task', 'verify', a)
        self.assertEqual(va['state'], 'accepted', va)
        self.assertIs(va['published'], True, va)
        self.assertEqual((self.repo / 'src/a.py').read_text(), SIX)
        self.assertEqual((self.repo / 'src/b.py').read_text(), FIVE)
        snap = tree_bytes(self.repo)
        # integrated candidate for B is a=6,b=6 = 12: the check fails, B is conflicted, canonical has A only
        rc, vb = self.call('task', 'verify', b)
        if rc == 3:
            self.assertEqual(vb.get('code'), 'checks_failed', vb)
        else:
            self.assertEqual(rc, 0, vb)
            self.assertIs(vb.get('published'), False, vb)
        self.assertEqual(self.show(b)['state'], 'conflicted')
        self.assert_canonical_untouched(snap)
        self.assertEqual((self.repo / 'src/a.py').read_bytes(), SIX.encode())
        self.assertEqual((self.repo / 'src/b.py').read_bytes(), FIVE.encode())

    def test_07_crashed_worker(self):
        tid = self.task('--write', 'src/a.py')
        w1, e1, _ = self.claim(tid, 'crashed', lease=1)
        write(w1, 'src/a.py', SIX)
        time.sleep(2.0)  # no heartbeat past the 1 s TTL
        rec = self.ok('task', 'recover', tid)
        self.assertIn('expired', json.dumps(rec).lower(), rec)
        self.assertIn('claim', rec['next_action'], rec)
        self.assertIn(tid, rec['next_action'], rec)
        # reclaim: fresh worktree, new epoch, edit transferred and hash-verified
        w2, e2, cl = self.claim(tid, 'rescuer')
        self.assertGreater(e2, e1)
        self.assertNotEqual(w2, w1)
        self.assertIn('src/a.py', json.dumps(cl['recovered']), cl)
        self.assertEqual(list(cl.get('rebase_needed', [])), [], cl)
        self.assertEqual((w2 / 'src/a.py').read_text(), SIX)
        self.assertTrue((w1 / 'src/a.py').is_file(), 'retired worktree was deleted')
        self.assertEqual((w1 / 'src/a.py').read_text(), SIX)
        rows = {Path(x['path']): x['status'] for x in self.ok('task', 'recover', tid)['worktrees']}
        self.assertEqual(rows.get(w1), 'retired', rows)
        self.assertEqual(rows.get(w2), 'active', rows)
        # a write into the old path is denied by the hook...
        p = self.hook(claude_payload(w1 / 'src/a.py', w1), cwd=w1)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertEqual(p.stdout, '')
        self.assertIn('retired', p.stderr)
        self.assertIn(tid, p.stderr)
        # ...and even a hook-bypassing write there never reaches the new owner's bundle
        write(w1, 'src/a.py', 'VALUE = 7\n')
        sub = self.submit(tid, 'rescuer', e2)
        self.assertEqual(sub['state'], 'submitted', sub)
        ch = [c for c in sub['changes'] if c.get('path') == 'src/a.py']
        self.assertEqual(len(ch), 1, sub)
        self.assertEqual(_first(ch[0], 'after_sha256', 'after'), sha(SIX), ch)
        self.assertNotIn(sha('VALUE = 7\n'), json.dumps(sub))
        v = self.ok('task', 'verify', tid)
        self.assertEqual(v['state'], 'accepted', v)
        self.assertEqual((self.repo / 'src/a.py').read_text(), SIX)
        self.assertEqual((w1 / 'src/a.py').read_text(), 'VALUE = 7\n')  # still retained, still ignored

    def test_08_reviewer_cannot_mutate(self):
        tid = self.task('--write', 'src/a.py')
        w, _, _ = self.claim(tid, 'builder')
        # Claude dialect: Write into canonical while a task is active (WCP_ROLE unset) -> deny
        p = self.hook(claude_payload(self.repo / 'src/a.py', self.repo), cwd=self.repo)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertEqual(p.stdout, '')
        self.assertTrue(p.stderr.strip(), 'deny without a reason on stderr')
        # Claude dialect: worktree write outside scope -> deny, scope named
        p = self.hook(claude_payload(w / 'src/b.py', w), cwd=w)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertEqual(p.stdout, '')
        self.assertIn('src/a.py', p.stderr)
        # Claude dialect: in-scope worktree write -> allow, silent
        p = self.hook(claude_payload(w / 'src/a.py', w, tool='Edit'), cwd=w)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, '')
        # Grok dialect (toolName/toolInput/workspaceRoot): canonical write -> deny
        p = self.hook(grok_payload(self.repo / 'src/a.py', self.repo), cwd=self.repo)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertEqual(p.stdout, '')
        # Codex apply_patch: `*** Update File:` naming an out-of-scope path, relative to the worktree cwd -> deny
        p = self.hook(codex_apply_patch_payload('src/b.py', w), cwd=w)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertEqual(p.stdout, '')
        self.assertIn('src/a.py', p.stderr)
        # nothing above touched canonical
        self.assertEqual((self.repo / 'src/a.py').read_text(), FIVE)
        self.assertEqual((self.repo / 'src/b.py').read_text(), FIVE)

    def test_09_cleanup_never_discards(self):
        tid = self.task('--write', 'src/a.py')
        w, e, _ = self.claim(tid, 'builder')
        write(w, 'src/a.py', SIX)
        self.submit(tid, 'builder', e)
        self.assertEqual(self.ok('task', 'verify', tid)['state'], 'accepted')

        def refuses(expect_prefix):
            err = self.refused('unclean_worktree', 'task', 'cleanup', tid)
            detail = err.get('detail') or {}
            self.assertTrue(any(str(x).startswith(expect_prefix) for x in detail.get('paths', [])),
                            '%s not listed in %s' % (expect_prefix, err))
            self.assertEqual(Path(detail.get('worktree', '')).resolve(), w.resolve(), err)
            self.assertTrue(w.is_dir(), 'cleanup removed a worktree it refused')

        extra = w / 'notes.txt'
        write(w, 'notes.txt', 'scratch\n')
        refuses('notes.txt')
        extra.unlink()
        os.symlink('README.md', str(extra))
        refuses('notes.txt')
        extra.unlink()
        write(w, '__pycache__/x.pyc', b'\x00cache')  # git-ignored residue still blocks
        refuses('__pycache__')
        shutil.rmtree(str(w / '__pycache__'))
        self.ok('task', 'cleanup', tid)
        self.assertFalse(w.exists(), 'pristine worktree was not removed')
        self.assertEqual((self.repo / 'src/a.py').read_text(), SIX)

    def test_10_lost_messages(self):
        tid = self.task('--write', 'src/a.py')
        w, e, _ = self.claim(tid, 'builder')
        write(w, 'src/a.py', SIX)
        sub = self.submit(tid, 'builder', e)
        sid = sub['submission_id']
        # "the message" from builder to integrator is never sent; a fresh process reads only the DB
        rec = self.ok('task', 'recover', tid)
        self.assertEqual(rec.get('state', _task(rec).get('state')), 'submitted', rec)
        self.assertIn('verify', rec['next_action'], rec)
        self.assertIn(tid, rec['next_action'], rec)
        v = self.ok('task', 'verify', tid)
        self.assertEqual(v['state'], 'accepted', v)
        self.assertIs(v['published'], True, v)
        self.assertEqual((self.repo / 'src/a.py').read_text(), SIX)
        self.assertEqual(self.show(tid)['state'], 'accepted')
        ev = self.ok('events', tid)
        events = ev.get('events', ev)
        text = json.dumps(events)
        self.assertIn('publish_finished', text)
        self.assertIn(sid, text)      # the accepted artifact names the submission...
        self.assertIn(sha(SIX), text)  # ...and its after-hash
        # an accepted task is integrator-closed: a second verify is bad_state and changes nothing
        snap = tree_bytes(self.repo)
        self.refused('bad_state', 'task', 'verify', tid)
        self.assert_canonical_untouched(snap)
        self.assertEqual(self.show(tid)['state'], 'accepted')


if __name__ == '__main__':
    unittest.main()
