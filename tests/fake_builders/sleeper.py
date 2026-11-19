"""Sleeps forever with a grandchild sleeper (pid recorded in control[key]['pidfile']); for group-kill tests."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import _common as c

ctl = c.control()
c.mark(ctl)
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])
Path(c.mine(ctl)['pidfile']).write_text(str(child.pid))
time.sleep(600)
