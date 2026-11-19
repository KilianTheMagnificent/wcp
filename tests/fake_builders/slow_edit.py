"""Marks, sleeps control[key]['sleep'] seconds, then edits in scope and exits 0 (runner-kill/adoption tests)."""
from __future__ import annotations

import time

import _common as c

ctl = c.control()
c.mark(ctl)
me = c.mine(ctl)
time.sleep(float(me.get('sleep', 3)))
for rel, text in me.get('write', {}).items():
    c.write(rel, text)
