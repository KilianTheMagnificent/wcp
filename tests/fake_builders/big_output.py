"""Prints control[key]['mib'] MiB of lines, then a marker line, then records the largest builder log it
can see (the runner must keep the log under the cap while we run) into control[key]['out']; edits in scope."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import _common as c

ctl = c.control()
c.mark(ctl)
me = c.mine(ctl)
line = ('x' * 99) + '\n'
for _ in range(int(me.get('mib', 2) * 1024 * 1024 / len(line))):
    sys.stdout.write(line)
print('api_key = ' + '01234567' + '89abcdef', flush=True)  # synthetic secret-shaped line inside the tail
if me.get('marker'):
    print(me['marker'], flush=True)
time.sleep(1.0)  # let the wrapper drain everything before we look at the log
key = os.environ['WCP_KEY']
sizes = [p.stat().st_size for p in Path(os.environ['WCP_HOME']).glob('repos/*/runs/*/builder-%s-*.log' % key)]
Path(me['out']).write_text(str(max(sizes) if sizes else -1))
for rel, text in me.get('write', {}).items():
    c.write(rel, text)
