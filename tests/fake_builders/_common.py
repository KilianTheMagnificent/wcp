"""Shared plumbing for the fake builders: control file (argv[1]), env, per-attempt marker files."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def control() -> dict:
    if len(sys.argv) > 1 and sys.argv[1] != '-':
        return json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    return {}


def mine(ctl: dict) -> dict:
    return ctl.get(os.environ.get('WCP_KEY', ''), {})


def worktree() -> Path:
    return Path(os.environ['WCP_WORKTREE'])


def mark(ctl: dict) -> int:
    """Write marks_dir/<key>-e<epoch>-<pid> (one per builder process: a double dispatch shows as two marks
    for one epoch); return this builder's attempt index (0-based) for its key."""
    d = Path(ctl['marks_dir'])
    d.mkdir(parents=True, exist_ok=True)
    key = os.environ['WCP_KEY']
    n = len([p for p in d.iterdir() if p.name.startswith(key + '-e')])
    (d / ('%s-e%s-%d' % (key, os.environ['WCP_EPOCH'], os.getpid()))).write_text(str(n))
    return n


def write(rel: str, text: str) -> None:
    p = worktree() / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8')
