"""Records every WCP_* variable and argv into control[key]['out']; exits 1 unless WCP_ROLE=builder and exactly
the six builder variables (plus WCP_ROLE) are present; edits in scope so the task can be accepted."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import _common as c

EXPECTED = {'WCP_HOME', 'WCP_TASK', 'WCP_OWNER', 'WCP_EPOCH', 'WCP_WORKTREE', 'WCP_KEY', 'WCP_ROLE'}
ctl = c.control()
c.mark(ctl)
me = c.mine(ctl)
wcp = {k: v for k, v in os.environ.items() if k.startswith('WCP_')}
Path(me['out']).write_text(json.dumps({
    'env': wcp, 'all_keys': sorted(os.environ.keys()), 'argv': sys.argv, 'cwd': os.getcwd(),
}))
for rel, text in me.get('write', {}).items():
    c.write(rel, text)
sys.exit(0 if (set(wcp) == EXPECTED and wcp['WCP_ROLE'] == 'builder') else 1)
