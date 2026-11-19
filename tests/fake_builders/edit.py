"""Step-driven builder: control[key]['attempts'][i] applies on the i-th dispatch (last step repeats).
Step keys: write {rel: text} (worktree), canonical_write {rel: text} (control['root']; simulates a human edit
of canonical), symlink {rel: target}, print [lines], sleep S, exit N (default 0)."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import _common as c

ctl = c.control()
n = c.mark(ctl)
steps = c.mine(ctl).get('attempts') or [{}]
step = steps[min(n, len(steps) - 1)]
for rel, text in step.get('write', {}).items():
    c.write(rel, text)
for rel, text in step.get('canonical_write', {}).items():
    (Path(ctl['root']) / rel).write_text(text, encoding='utf-8')
for rel, target in step.get('symlink', {}).items():
    os.symlink(target, c.worktree() / rel)
for line in step.get('print', []):
    print(line, flush=True)
time.sleep(step.get('sleep', 0))
sys.exit(int(step.get('exit', 0)))
