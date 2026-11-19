"""Git plumbing: repo identity, worktree provisioning, listing + hashing, checks runner."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import signal
import stat
import subprocess
import time
from pathlib import Path

from . import paths
from .db import DEFAULT_HOME, ProtocolError


class GitError(Exception):
    pass


def run_git(args, cwd, check=True, *, text=True, input=None):
    """Run git with captured output. text=False returns bytes (needed for binary patches).

    --no-optional-locks on every invocation: reads against a canonical checkout a human is using
    (status, diff HEAD, ls-files, worktree list, rev-parse) must never take .git/index.lock.
    """
    p = subprocess.run(['git', '--no-optional-locks', *args], cwd=str(cwd), input=input, capture_output=True,
                       text=text, encoding='utf-8' if text else None,
                       stdin=None if input is not None else subprocess.DEVNULL)
    if check and p.returncode != 0:
        err = p.stderr if text else p.stderr.decode('utf-8', 'replace')
        raise GitError('git %s failed (%d): %s' % (' '.join(args), p.returncode, err.strip()))
    return p


def _z(out):
    return [s for s in out.split('\0') if s]


def canonical_root(start):
    try:
        p = run_git(['rev-parse', '--git-common-dir'], start, check=False)
    except OSError:
        p = None
    if p is None or p.returncode != 0:
        raise ProtocolError('not_a_repo', 'not a git repository: %s' % start, {'path': str(start)})
    common = (Path(start) / p.stdout.strip()).resolve()
    if common.name != '.git':  # bare repo or submodule: refuse rather than guess the root
        raise ProtocolError('not_a_repo', 'unsupported repository layout: %s' % common, {'path': str(start)})
    return common.parent


def head_revision(root):
    p = run_git(['rev-parse', '--verify', '-q', 'HEAD^{commit}'], root, check=False)
    if p.returncode != 0:
        raise ProtocolError('no_commits', 'HEAD has no commits in %s' % root, {'root': str(root)})
    return p.stdout.strip()


def list_entries(tree, link_dirs=()):
    """path -> (sha256, mode) for tracked + untracked-non-ignored entries present on disk.

    Regular files: (sha256, '100644'|'100755'); symlinks: ('symlink:<sha256 of target>', 'symlink');
    anything else: ('unsupported', 'unsupported'). Sensitive paths and the named link_dirs are
    excluded. Deleted-but-tracked paths are absent. Keys sorted.
    """
    tree = Path(tree)
    names = set(_z(run_git(['ls-files', '-z'], tree).stdout))
    names |= set(_z(run_git(['ls-files', '--others', '--exclude-standard', '-z'], tree).stdout))
    links = {paths.normalize_repo_path(d) for d in link_dirs}
    out = {}
    for rel in sorted(paths.normalize_repo_path(n) for n in names):
        if rel in links or paths.is_sensitive(rel):
            continue
        try:
            st = os.lstat(tree / rel)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(st.st_mode):
            # ponytail: whole-file read; fine for source trees, not for multi-GB assets
            out[rel] = (paths.sha256_bytes(paths.read_regular(tree, rel)), paths.git_mode(st.st_mode))
        elif stat.S_ISLNK(st.st_mode):
            target = os.readlink(os.fsencode(tree / rel))
            out[rel] = ('symlink:' + paths.sha256_bytes(target), 'symlink')
        else:
            out[rel] = ('unsupported', 'unsupported')
    return out


snapshot = list_entries


def sha256_file(path):
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise ProtocolError('unsupported_path', 'not a regular file: %s' % path, {'path': str(path)})
    h = hashlib.sha256()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def tracked_sensitive(root):
    """Sensitive-named paths in the index or in HEAD (both are what a checkout/overlay would copy)."""
    names = set(_z(run_git(['ls-files', '-z'], root).stdout))
    p = run_git(['ls-tree', '-r', '-z', '--name-only', 'HEAD'], root, check=False)
    if p.returncode == 0:
        names |= set(_z(p.stdout))
    return sorted(n for n in names if paths.is_sensitive(n))


def provision_worktree(root, dest, link_dirs):
    """Spec steps 1-4 (no DB): worktree add, overlay replay, link_dirs. Cleans up on failure."""
    root = canonical_root(root)
    dest = Path(dest)
    head_revision(root)
    bad = tracked_sensitive(root)
    if bad:
        raise ProtocolError(
            'sensitive_path',
            'tracked credential-shaped paths refuse provisioning (untrack them with git rm --cached '
            'and keep them ignored): ' + ', '.join(bad), {'paths': bad})
    if os.path.lexists(dest):
        raise ProtocolError('bad_state', 'worktree path already exists: %s' % dest, {'path': str(dest)})
    # hooksPath=/dev/null: the repo's post-checkout hook (husky, git-lfs, core.hooksPath) is undeclared code
    run_git(['-c', 'core.hooksPath=/dev/null', 'worktree', 'add', '--detach', str(dest), 'HEAD'], root)
    try:
        os.chmod(dest, 0o700)
        head = head_revision(dest)
        skipped = _replay_overlay(root, dest)
        _link_dirs(root, dest, link_dirs)
    except BaseException:
        remove_worktree(root, dest, force=True)  # provisioning scratch never handed to a builder
        raise
    return {'head': head, 'sensitive_skipped': skipped}


def _replay_overlay(root, dest):
    # ponytail: submodule pointers, clean/smudge filters and autocrlf are not replayed
    patch = run_git(['diff', 'HEAD', '--binary', '--no-color', '--no-ext-diff', '--no-textconv',
                     '--no-renames', '--ignore-submodules=all', '--src-prefix=a/', '--dst-prefix=b/'],
                    root, text=False).stdout
    if patch.strip():
        p = run_git(['apply', '--whitespace=nowarn', '-'], dest, check=False, text=False, input=patch)
        if p.returncode != 0:
            raise GitError('overlay replay failed: ' + p.stderr.decode('utf-8', 'replace').strip())
    skipped = 0
    for rel in _z(run_git(['ls-files', '--others', '--exclude-standard', '-z'], root).stdout):
        rel = paths.normalize_repo_path(rel)
        if paths.is_sensitive(rel):
            skipped += 1
            continue
        st = os.lstat(root / rel)
        if not stat.S_ISREG(st.st_mode):
            continue
        paths.atomic_write(dest, rel, paths.read_regular(root, rel), paths.git_mode(st.st_mode))
    return skipped


def _link_dirs(root, dest, link_dirs):
    for d in link_dirs:
        rel = paths.normalize_repo_path(d)
        target = root / rel
        if not target.is_dir():
            continue
        parent, name = os.path.split(rel)
        fd = paths.open_dir_chain(dest, parent, True)
        try:
            os.symlink(str(target), name, dir_fd=fd)
        finally:
            os.close(fd)


def registration(root, path):
    """'active' (listed and exists), 'absent' (neither), 'unknown' (git failed or they disagree)."""
    path = Path(path)
    exists = path.is_dir()
    try:
        p = run_git(['worktree', 'list', '--porcelain', '-z'], root, check=False)
    except OSError:
        return 'unknown'
    if p.returncode != 0:
        return 'unknown'
    real = os.path.realpath(path)
    listed = any(os.path.realpath(line[len('worktree '):]) == real
                 for line in p.stdout.split('\0') if line.startswith('worktree '))
    if listed and exists:
        return 'active'
    if not listed and not exists:
        return 'absent'
    return 'unknown'


def remove_worktree(root, path, *, force=False):
    """Remove an 'active' worktree under WCP_HOME. False means nothing was removed and the
    directory, if it existed, still exists. force only from the spec's two proven paths."""
    path = Path(path)
    home = os.path.realpath(DEFAULT_HOME())
    real = os.path.realpath(path)
    if real == home or os.path.commonpath([home, real]) != home:
        return False
    reg = registration(root, path)
    if reg == 'active':
        if not force and is_dirty(path):
            return False
        run_git(['worktree', 'remove'] + (['--force'] if force else []) + [str(path)], root, check=False)
    # 'absent': nothing on disk and nothing registered; 'unknown': leave it for a human
    return not os.path.lexists(path)


def _check_cwd(raw) -> str:
    if raw is None or raw in ('', '.'):
        return '.'
    if not isinstance(raw, str):
        raise ProtocolError('unsafe_path', 'unsafe path %r' % (raw,), {'path': raw})
    return paths.normalize_repo_path(raw)


def read_agent_tools_config(root):
    f = Path(root) / '.agent-tools.json'
    if not f.is_file():
        return None
    cfg = json.loads(f.read_text('utf-8'))
    wanted = set(cfg.get('completion', {}).get('required_check_ids', []))
    checks = []
    for c in cfg.get('checks', []):
        if c.get('id') in wanted and c.get('required', False) and not c.get('skip', False):
            checks.append({'argv': list(c['argv']) if c.get('argv') else None,
                           'cmd': c.get('cmd') or None,
                           'cwd': _check_cwd(c.get('cwd')),
                           'timeout_s': int(c.get('timeout_seconds') or 900)})
    return {'checks': checks, 'preserve_roots': list(cfg.get('preserve_roots', []))}


def _kill_check_group(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        p.kill()
    except ProcessLookupError:
        pass
    p.wait()


def run_check(check, cwd):
    """Run one declared check uncontained; the receipt is an untrusted observation."""
    argv, cmd = check.get('argv'), check.get('cmd')
    if not argv and not cmd:
        raise ValueError('check has neither argv nor cmd')
    display = shlex.join(argv) if argv else cmd
    rel_cwd = _check_cwd(check.get('cwd'))
    workdir = Path(cwd) if rel_cwd == '.' else Path(cwd) / rel_cwd
    timeout = check.get('timeout_s') or 900
    t0 = time.monotonic()
    with subprocess.Popen(argv or cmd, shell=not argv, cwd=str(workdir), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True) as p:
        try:
            out, _ = p.communicate(timeout=timeout)
            code, out = p.returncode, out or b''
        except subprocess.TimeoutExpired as e:
            _kill_check_group(p)
            code, out = -1, (e.stdout or b'') + ('\n[wcp: timed out after %ss]' % timeout).encode()
        except BaseException:
            _kill_check_group(p)
            raise
    return {'cmd': display, 'exit_code': code,
            'duration_ms': int((time.monotonic() - t0) * 1000),
            'output_tail': out.decode('utf-8', 'replace')[-4000:],
            'trust': 'untrusted_observation'}


def is_dirty(root):
    return bool(run_git(['status', '--porcelain', '-z'], root).stdout.strip('\0 \n'))
