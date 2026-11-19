from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import wcp  # noqa: E402


class VersionTests(unittest.TestCase):
    def test_json_version_matches_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, WCP_HOME=os.path.join(tmp, 'home'),
                       PYTHONPATH=str(REPO) + os.pathsep + os.environ.get('PYTHONPATH', ''))
            p = subprocess.run([sys.executable, '-m', 'wcp', '--json', 'version'],
                               cwd=str(REPO), env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertTrue(out['ok'], out)
        self.assertEqual(out['version'], wcp.__version__)

    def test_version_literal(self):
        self.assertEqual(wcp.__version__, '0.3.0')


if __name__ == '__main__':
    unittest.main()
