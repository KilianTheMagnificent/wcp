from __future__ import annotations

import hashlib
import inspect
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


from fixtures import commit, make_home, make_repo, run, write
from wcp import gitx
from wcp.db import ProtocolError


class GitxBase(unittest.TestCase):
    def setUp(self):
        self.home = make_home()
        self.addCleanup(shutil.rmtree, str(self.home), True)
        self.n = 0

    def repo(self, with_dirty=False):
        r = make_repo(with_dirty)
        self.addCleanup(shutil.rmtree, str(r), True)
        return r

    def tmp(self):
        d = Path(tempfile.mkdtemp(prefix='wcp-tmp-')).resolve()
        self.addCleanup(shutil.rmtree, str(d), True)
        return d

    def dest(self):
        self.n += 1
        return self.home / 'repos' / 'r' / 'worktrees' / ('t-%06d-e%d' % (self.n, self.n))


class ProvisionTests(GitxBase):
    def test_dirty_canonical_replays_overlay_and_skips_env(self):
        repo = self.repo()
        write(repo, 'data.bin', bytes(range(256)))
        commit(repo, 'binary')
        # dirty canonical: tracked edit, binary edit, mode-only edit, tracked deletion, untracked, .env
        write(repo, 'README.md', '# fixture (edited)\n')
        write(repo, 'data.bin', bytes(reversed(range(256))))
        os.chmod(repo / 'tests/check.py', 0o755)
        os.unlink(repo / 'src/b.py')
        write(repo, 'notes.txt', 'untracked notes\n')
        write(repo, '.env', 'FIXTURE_NOT_A_SECRET=1\n')
        dest = self.dest()
        info = gitx.provision_worktree(repo, dest, [])
        self.assertEqual(info['sensitive_skipped'], 1)
        self.assertEqual(info['head'], gitx.head_revision(repo))
        entries = gitx.list_entries(dest)
        self.assertEqual(entries, gitx.list_entries(repo))
        self.assertIn('notes.txt', entries)
        self.assertNotIn('src/b.py', entries)
        self.assertEqual(entries['tests/check.py'][1], '100755')
        self.assertEqual((dest / 'README.md').read_text(), '# fixture (edited)\n')
        self.assertEqual((dest / 'data.bin').read_bytes(), bytes(reversed(range(256))))
        self.assertFalse((dest / 'src/b.py').exists())
        self.assertFalse(os.path.lexists(dest / '.env'))
        self.assertEqual(gitx.registration(repo, dest), 'active')
        self.assertEqual(stat_mode(dest), 0o700)
        # canonical untouched
        self.assertTrue((repo / '.env').exists())
        self.assertEqual((repo / 'README.md').read_text(), '# fixture (edited)\n')

    def test_clean_canonical_matches_head(self):
        repo = self.repo()
        dest = self.dest()
        info = gitx.provision_worktree(repo, dest, [])
        self.assertEqual(info['sensitive_skipped'], 0)
        self.assertEqual(gitx.list_entries(dest), gitx.list_entries(repo))
        self.assertFalse(gitx.is_dirty(dest))
        self.assertEqual(gitx.canonical_root(dest), repo)

    def test_tracked_sensitive_refuses_without_override(self):
        repo = self.repo()
        write(repo, 'config/credentials.json', '{}\n')
        commit(repo, 'oops')
        dest = self.dest()
        with self.assertRaises(ProtocolError) as cm:
            gitx.provision_worktree(repo, dest, [])
        self.assertEqual(cm.exception.code, 'sensitive_path')
        self.assertEqual(cm.exception.detail['paths'], ['config/credentials.json'])
        self.assertFalse(os.path.lexists(dest))
        self.assertEqual(gitx.registration(repo, dest), 'absent')
        self.assertEqual(gitx.tracked_sensitive(repo), ['config/credentials.json'])
        self.assertNotIn('allow_tracked_sensitive', inspect.signature(gitx.provision_worktree).parameters)

    def test_staged_uncommitted_sensitive_refuses(self):
        repo = self.repo()
        write(repo, '.env', 'FIXTURE_NOT_A_SECRET=1\n')
        run(['git', 'add', '.env'], repo)
        with self.assertRaises(ProtocolError) as cm:
            gitx.provision_worktree(repo, self.dest(), [])
        self.assertEqual(cm.exception.code, 'sensitive_path')
        self.assertEqual(cm.exception.detail['paths'], ['.env'])

    def test_link_dirs_only_when_named(self):
        repo = self.repo()
        write(repo, 'node_modules/x.js', '1\n')
        d1 = self.dest()
        gitx.provision_worktree(repo, d1, [])
        self.assertFalse(os.path.lexists(d1 / 'node_modules'))
        d2 = self.dest()
        gitx.provision_worktree(repo, d2, ['node_modules', 'absent-dir'])
        self.assertTrue(os.path.islink(d2 / 'node_modules'))
        self.assertEqual(os.path.realpath(d2 / 'node_modules'), str(repo / 'node_modules'))
        self.assertFalse(os.path.lexists(d2 / 'absent-dir'))
        self.assertNotIn('node_modules', gitx.list_entries(d2, ['node_modules']))
        self.assertEqual(gitx.list_entries(d2, ['node_modules']), gitx.list_entries(repo))

    def test_refusals(self):
        notrepo = self.tmp()
        with self.assertRaises(ProtocolError) as cm:
            gitx.canonical_root(notrepo)
        self.assertEqual(cm.exception.code, 'not_a_repo')
        with self.assertRaises(ProtocolError) as cm:
            gitx.provision_worktree(notrepo, self.dest(), [])
        self.assertEqual(cm.exception.code, 'not_a_repo')
        empty = self.tmp()
        run(['git', 'init', '-q'], empty)
        with self.assertRaises(ProtocolError) as cm:
            gitx.head_revision(empty)
        self.assertEqual(cm.exception.code, 'no_commits')
        with self.assertRaises(ProtocolError) as cm:
            gitx.provision_worktree(empty, self.dest(), [])
        self.assertEqual(cm.exception.code, 'no_commits')
        repo = self.repo()
        dest = self.dest()
        gitx.provision_worktree(repo, dest, [])
        with self.assertRaises(ProtocolError) as cm:
            gitx.provision_worktree(repo, dest, [])
        self.assertEqual(cm.exception.code, 'bad_state')
        self.assertTrue(dest.is_dir())

    def test_repo_hooks_do_not_run_during_provisioning(self):
        repo = self.repo()
        marker = self.tmp() / 'HOOK_RAN'
        write(repo, '.git/hooks/post-checkout', '#!/bin/sh\ntouch %s\n' % marker)
        os.chmod(repo / '.git/hooks/post-checkout', 0o755)
        # the hook is live: a plain worktree add fires it
        plain = self.tmp() / 'plain'
        run(['git', 'worktree', 'add', '--detach', str(plain), 'HEAD'], repo)
        self.assertTrue(marker.exists())
        marker.unlink()
        dest = self.dest()
        gitx.provision_worktree(repo, dest, [])
        self.assertFalse(marker.exists())
        self.assertEqual(gitx.registration(repo, dest), 'active')
        self.assertEqual(gitx.list_entries(dest), gitx.list_entries(repo))

    def test_failed_provisioning_cleans_up(self):
        repo = self.repo()
        write(repo, 'shared/x', 'x\n')
        commit(repo, 'tracked dir named like a link dir')
        dest = self.dest()
        with self.assertRaises(FileExistsError):
            gitx.provision_worktree(repo, dest, ['shared'])
        self.assertFalse(os.path.lexists(dest))
        self.assertEqual(gitx.registration(repo, dest), 'absent')


class RegistrationTests(GitxBase):
    def test_three_states(self):
        repo = self.repo()
        dest = self.dest()
        self.assertEqual(gitx.registration(repo, dest), 'absent')
        gitx.provision_worktree(repo, dest, [])
        self.assertEqual(gitx.registration(repo, dest), 'active')
        self.assertTrue(gitx.remove_worktree(repo, dest))
        self.assertFalse(dest.exists())
        self.assertEqual(gitx.registration(repo, dest), 'absent')
        self.assertEqual(gitx.registration(self.tmp(), dest), 'unknown')
        gitx.provision_worktree(repo, dest, [])
        shutil.rmtree(dest)
        self.assertEqual(gitx.registration(repo, dest), 'unknown')
        # an unknown path is left alone
        self.assertTrue(gitx.remove_worktree(repo, dest, force=True))  # nothing on disk to keep

    def test_remove_refuses_outside_home_and_dirty(self):
        repo = self.repo()
        outside = self.tmp() / 'wt'
        run(['git', 'worktree', 'add', '--detach', str(outside), 'HEAD'], repo)
        self.assertFalse(gitx.remove_worktree(repo, outside, force=True))
        self.assertTrue(outside.is_dir())
        self.assertEqual(gitx.registration(repo, outside), 'active')
        # canonical itself is never removable
        self.assertFalse(gitx.remove_worktree(repo, repo, force=True))
        self.assertTrue(repo.is_dir())
        dest = self.dest()
        gitx.provision_worktree(repo, dest, [])
        write(dest, 'src/a.py', 'VALUE = 6\n')
        self.assertTrue(gitx.is_dirty(dest))
        self.assertFalse(gitx.remove_worktree(repo, dest))
        self.assertTrue(dest.is_dir())
        self.assertEqual(gitx.registration(repo, dest), 'active')
        self.assertTrue(gitx.remove_worktree(repo, dest, force=True))
        self.assertFalse(os.path.lexists(dest))


class ListEntriesTests(GitxBase):
    def test_entries_modes_symlinks_and_exclusions(self):
        repo = self.repo()
        os.symlink('README.md', repo / 'link')
        os.chmod(repo / 'tests/check.py', 0o755)
        write(repo, '__pycache__/x.pyc', 'ignored\n')
        write(repo, '.env', 'FIXTURE_NOT_A_SECRET=1\n')
        write(repo, '.env.example', 'KEY=\n')
        write(repo, 'sub/deep/file.txt', 'deep\n')
        os.unlink(repo / 'README.md')
        os.mkfifo(repo / 'README.md')                       # tracked path became a non-regular entry
        os.mkfifo(repo / 'fifo')                             # untracked FIFO: git never lists it
        e = gitx.list_entries(repo)
        self.assertEqual(list(e), sorted(e))
        self.assertEqual(e['link'], ('symlink:' + hashlib.sha256(b'README.md').hexdigest(), 'symlink'))
        self.assertEqual(e['README.md'], ('unsupported', 'unsupported'))
        self.assertNotIn('fifo', e)
        self.assertEqual(e['tests/check.py'][1], '100755')
        self.assertEqual(e['src/a.py'], (hashlib.sha256(b'VALUE = 5\n').hexdigest(), '100644'))
        self.assertEqual(e['src/a.py'][0], gitx.sha256_file(repo / 'src/a.py'))
        self.assertIn('.env.example', e)
        self.assertIn('sub/deep/file.txt', e)
        self.assertNotIn('.env', e)
        self.assertNotIn('__pycache__/x.pyc', e)
        self.assertIs(gitx.snapshot, gitx.list_entries)
        os.unlink(repo / 'src/b.py')
        self.assertNotIn('src/b.py', gitx.list_entries(repo))
        with self.assertRaises(ProtocolError):
            gitx.sha256_file(repo / 'link')


class ChecksTests(GitxBase):
    def test_read_agent_tools_config(self):
        repo = self.repo()
        self.assertIsNone(gitx.read_agent_tools_config(repo))
        write(repo, '.agent-tools.json', '''{
  "kind": "RepositoryConfigV1", "schema_version": "1.0",
  "checks": [
    {"id": "full", "argv": ["python3", "tests/check.py"], "cwd": ".", "required": true, "skip": false, "timeout_seconds": 60},
    {"id": "skipped", "argv": ["false"], "required": true, "skip": true},
    {"id": "optional", "argv": ["true"], "required": false, "skip": false},
    {"id": "unlisted", "argv": ["true"], "required": true, "skip": false}
  ],
  "completion": {"required_check_ids": ["full", "skipped", "optional"]},
  "edit_roots": ["src"], "preserve_roots": ["docs/decisions"]
}''')
        cfg = gitx.read_agent_tools_config(repo)
        self.assertEqual(cfg['preserve_roots'], ['docs/decisions'])
        self.assertEqual(cfg['checks'], [
            {'argv': ['python3', 'tests/check.py'], 'cmd': None, 'cwd': '.', 'timeout_s': 60}])
        for bad_cwd in ('..', '../outside', 'src/../../outside', '/tmp', '.git'):
            write(repo, '.agent-tools.json', '''{
  "checks": [{"id": "bad", "cmd": "true", "cwd": "%s", "required": true}],
  "completion": {"required_check_ids": ["bad"]}
}''' % bad_cwd)
            with self.assertRaises(ProtocolError) as cm:
                gitx.read_agent_tools_config(repo)
            self.assertEqual(cm.exception.code, 'unsafe_path')

    def test_run_check_argv_cmd_timeout(self):
        repo = self.repo()
        r = gitx.run_check({'argv': [sys.executable, '-c', 'import os,sys; print(os.getcwd()); sys.exit(3)'],
                            'cmd': None, 'cwd': 'src', 'timeout_s': 30}, repo)
        self.assertEqual(r['exit_code'], 3)
        self.assertEqual(r['output_tail'].strip(), os.path.realpath(repo / 'src'))
        self.assertEqual(r['trust'], 'untrusted_observation')
        self.assertIn('-c', r['cmd'])
        self.assertGreaterEqual(r['duration_ms'], 0)
        r = gitx.run_check({'argv': None, 'cmd': 'echo out; echo err >&2; exit 2', 'cwd': '.', 'timeout_s': 30}, repo)
        self.assertEqual(r['exit_code'], 2)
        self.assertIn('out', r['output_tail'])
        self.assertIn('err', r['output_tail'])
        self.assertEqual(r['cmd'], 'echo out; echo err >&2; exit 2')
        r = gitx.run_check({'argv': [sys.executable, '-c', 'import time; print("start", flush=True); time.sleep(30)'],
                            'timeout_s': 1}, repo)
        self.assertEqual(r['exit_code'], -1)
        self.assertLess(r['duration_ms'], 10000)
        self.assertIn('timed out', r['output_tail'])
        r = gitx.run_check({'cmd': 'python3 tests/check.py'}, repo)
        self.assertEqual(r['exit_code'], 0)
        write(repo, 'src/a.py', 'VALUE = 6\n')
        write(repo, 'src/b.py', 'VALUE = 6\n')
        self.assertEqual(gitx.run_check({'cmd': 'python3 tests/check.py'}, repo)['exit_code'], 1)
        self.assertFalse((repo / 'src' / '__pycache__').exists())
        r = gitx.run_check({'cmd': 'python3 -c "print(\'y\' * 6000)"'}, repo)
        self.assertEqual(len(r['output_tail']), 4000)

    def test_run_check_validates_cwd_and_kills_process_group_on_timeout(self):
        repo = self.repo()
        for valid_cwd in (None, '', '.'):
            r = gitx.run_check({'cmd': 'pwd', 'cwd': valid_cwd}, repo)
            self.assertEqual(r['exit_code'], 0)
            self.assertEqual(r['output_tail'].strip(), os.path.realpath(repo))
        for bad_cwd in ('..', '../outside', 'src/../../outside', '/tmp', '.git', 'a\0b', 123):
            with self.subTest(bad_cwd=bad_cwd):
                with self.assertRaises(ProtocolError) as cm:
                    gitx.run_check({'cmd': 'true', 'cwd': bad_cwd}, repo)
                self.assertEqual(cm.exception.code, 'unsafe_path')
        pidfile = self.tmp() / 'grandchild.pid'
        script = (
            'import pathlib, subprocess, sys, time; '
            'c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]); '
            'pathlib.Path(%r).write_text(str(c.pid)); '
            'print("spawned", flush=True); '
            'time.sleep(60)'
        ) % str(pidfile)
        r = gitx.run_check({'argv': [sys.executable, '-c', script], 'timeout_s': 1}, repo)
        self.assertEqual(r['exit_code'], -1)
        self.assertIn('spawned', r['output_tail'])
        self.assertIn('timed out', r['output_tail'])
        grandchild_pid = int(pidfile.read_text())
        deadline = time.monotonic() + 2.0
        while True:
            try:
                os.kill(grandchild_pid, 0)
            except ProcessLookupError:
                break
            if time.monotonic() >= deadline:
                self.fail('grandchild process %d survived timeout kill' % grandchild_pid)
            time.sleep(0.02)


class NoOptionalLocksTests(GitxBase):
    def test_canonical_reads_never_take_index_lock(self):
        repo = self.repo()
        index = repo / '.git' / 'index'
        os.utime(repo / 'src/a.py', (0, 0))  # stat-dirty: a refreshing read rewrites the index
        before = index.stat().st_mtime_ns
        with mock.patch.object(gitx.subprocess, 'run', wraps=subprocess.run) as spy:
            gitx.is_dirty(repo)
            gitx.list_entries(repo)
            gitx.head_revision(repo)
            gitx.canonical_root(repo)
            gitx.registration(repo, self.dest())
            self.assertFalse(os.path.lexists(repo / '.git' / 'index.lock'))
        self.assertTrue(spy.call_args_list)
        for call in spy.call_args_list:
            argv = call.args[0]
            self.assertEqual(argv[0], 'git')
            self.assertEqual(argv[1:2], ['--no-optional-locks'], argv)
        self.assertFalse(os.path.lexists(repo / '.git' / 'index.lock'))
        self.assertEqual(index.stat().st_mtime_ns, before)
        # control: the same read without the flag does refresh (rewrite) the canonical index
        run(['git', 'status', '--porcelain'], repo)
        self.assertNotEqual(index.stat().st_mtime_ns, before)


def stat_mode(p):
    return os.stat(p).st_mode & 0o777


if __name__ == '__main__':
    unittest.main()
