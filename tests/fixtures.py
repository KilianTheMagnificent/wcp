"""Shared test fixtures: temp WCP_HOME and a temp git repo with a tiny checked project."""
from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

CHECK_PY = '''import runpy, sys
sys.dont_write_bytecode = True
a = runpy.run_path("src/a.py")["VALUE"]
b = runpy.run_path("src/b.py")["VALUE"]
print("a+b =", a + b)
sys.exit(0 if a + b <= 11 else 1)
'''

_GIT_HOME = None


def _git_home():
    global _GIT_HOME
    if _GIT_HOME is None:
        _GIT_HOME = Path(tempfile.mkdtemp(prefix='wcp-githome-')).resolve()
        atexit.register(shutil.rmtree, str(_GIT_HOME), True)
    return _GIT_HOME


def git_env():
    """Fixed author/committer; user and system gitconfig ignored."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update({
        'HOME': str(_git_home()),
        'XDG_CONFIG_HOME': str(_git_home()),
        'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': '/dev/null',
        'GIT_AUTHOR_NAME': 'wcp-test',
        'GIT_AUTHOR_EMAIL': 'wcp-test@example.invalid',
        'GIT_COMMITTER_NAME': 'wcp-test',
        'GIT_COMMITTER_EMAIL': 'wcp-test@example.invalid',
        'GIT_AUTHOR_DATE': '2026-01-01T00:00:00+0000',
        'GIT_COMMITTER_DATE': '2026-01-01T00:00:00+0000',
    })
    return env


def make_home():
    """Temp WCP_HOME (0700), exported; also makes wcp's own git subprocesses hermetic."""
    home = Path(tempfile.mkdtemp(prefix='wcp-home-')).resolve()
    os.chmod(home, 0o700)
    os.environ['WCP_HOME'] = str(home)
    for k in [k for k in os.environ if k.startswith('GIT_')]:
        del os.environ[k]
    os.environ.update({k: v for k, v in git_env().items() if k.startswith('GIT_')})
    return home


def run(cmd, cwd, env=None, check=True):
    p = subprocess.run(cmd, cwd=str(cwd), env=env or git_env(), capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError('%s failed (%d): %s' % (cmd, p.returncode, p.stderr.strip()))
    return p


def write(repo, rel, text):
    p = Path(repo) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        p.write_bytes(text)
    else:
        p.write_text(text, encoding='utf-8')
    return p


def commit(repo, message='update'):
    run(['git', 'add', '-A'], repo)
    run(['git', '-c', 'commit.gpgsign=false', 'commit', '-q', '--allow-empty', '-m', message], repo)


def make_repo(with_dirty=False):
    """Temp git repo: README.md, src/a.py, src/b.py (VALUE = 5), tests/check.py (a+b <= 11),
    .gitignore, one commit. with_dirty: README edited, untracked notes.txt, untracked .env."""
    repo = Path(tempfile.mkdtemp(prefix='wcp-repo-')).resolve()
    run(['git', 'init', '-q'], repo)
    write(repo, 'README.md', '# fixture\n')
    write(repo, 'src/a.py', 'VALUE = 5\n')
    write(repo, 'src/b.py', 'VALUE = 5\n')
    write(repo, 'tests/check.py', CHECK_PY)
    write(repo, '.gitignore', '__pycache__/\nnode_modules/\n')
    commit(repo, 'init')
    if with_dirty:
        write(repo, 'README.md', '# fixture (edited)\n')
        write(repo, 'notes.txt', 'untracked notes\n')
        write(repo, '.env', 'FIXTURE_NOT_A_SECRET=1\n')
    return repo
