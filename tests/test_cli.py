from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import wcp

REPO = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r'^wcp (\d+\.\d+\.\d+) \(python (\d+\.\d+\.\d+)\)$')


def run(args):
    """Run `python3 -m wcp ...` from a temp cwd that is not a git repo."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, WCP_HOME=os.path.join(tmp, 'home'),
                   PYTHONPATH=str(REPO) + os.pathsep + os.environ.get('PYTHONPATH', ''))
        cwd = os.path.join(tmp, 'cwd')
        os.mkdir(cwd)
        return subprocess.run([sys.executable, '-m', 'wcp'] + args, cwd=cwd, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)


class VersionTests(unittest.TestCase):
    def test_not_a_git_repo(self):
        # guards the premise: the temp cwd really has no repo above it
        p = run(['status'])
        self.assertNotEqual(p.returncode, 0)

    def test_human(self):
        p = run(['version'])
        self.assertEqual(p.returncode, 0, p.stderr)
        m = VERSION_RE.match(p.stdout.strip())
        self.assertIsNotNone(m, p.stdout)
        self.assertEqual(m.group(1), wcp.__version__)
        self.assertEqual(m.group(2), '%d.%d.%d' % sys.version_info[:3])

    def test_json(self):
        p = run(['--json', 'version'])
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out, {'ok': True, 'version': wcp.__version__,
                               'python': '%d.%d.%d' % sys.version_info[:3]})

    def test_json_after_subcommand(self):
        p = run(['version', '--json'])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(json.loads(p.stdout)['ok'])


if __name__ == '__main__':
    unittest.main()
