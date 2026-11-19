"""Bundles: manifest + content-addressed blob store; diff vs baseline; apply with hash gates."""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import db, gitx, paths
from .db import ProtocolError


@dataclass
class Change:
    path: str
    op: str  # create | modify | chmod | delete
    before: Optional[str]
    before_mode: Optional[str]
    after: Optional[str]
    after_mode: Optional[str]

    def to_dict(self):
        return {'path': self.path, 'op': self.op,
                'before_sha256': self.before, 'before_mode': self.before_mode,
                'after_sha256': self.after, 'after_mode': self.after_mode}

    @classmethod
    def from_dict(cls, d):
        return cls(d['path'], d['op'], d['before_sha256'], d['before_mode'],
                   d['after_sha256'], d['after_mode'])


def _special(entry):
    return entry[1] in ('symlink', 'unsupported')


def diff_against_baseline(tree, baseline, link_dirs=()):
    """Changes whose identity (sha256, mode) differs from baseline. Equal hash + different
    exec bit -> 'chmod'. Any change touching a symlink/unsupported entry -> unsupported_path."""
    current = gitx.list_entries(tree, link_dirs)
    changes, bad = [], []
    for path in sorted(set(baseline) | set(current)):
        paths.normalize_repo_path(path)
        b, c = baseline.get(path), current.get(path)
        b = tuple(b) if b else None
        if b == c:
            continue
        if (b and _special(b)) or (c and _special(c)):
            bad.append(path)
        elif b is None:
            changes.append(Change(path, 'create', None, None, c[0], c[1]))
        elif c is None:
            changes.append(Change(path, 'delete', b[0], b[1], None, None))
        else:
            changes.append(Change(path, 'chmod' if b[0] == c[0] else 'modify', b[0], b[1], c[0], c[1]))
    if bad:
        raise ProtocolError('unsupported_path', 'non-regular entries changed: ' + ', '.join(bad), {'paths': bad})
    return changes


def _write_private(dest, data):
    tmp = dest.with_name(dest.name + '.tmp%d' % os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, dest)


def write_bundle(bundle_dir, tree, changes):
    bundle_dir = Path(bundle_dir)
    blobs = bundle_dir / 'blobs'
    db.ensure_private(bundle_dir, True)
    db.ensure_private(blobs, True)
    for ch in changes:
        if ch.after is None:
            continue
        data = paths.read_regular(tree, ch.path)
        if paths.sha256_bytes(data) != ch.after:
            raise ProtocolError('bad_state', 'worktree changed during submit: %s' % ch.path, {'path': ch.path})
        _write_private(blobs / ch.after, data)
    manifest = {'changes': [c.to_dict() for c in changes], 'created_at': db.now()}
    _write_private(bundle_dir / 'manifest.json', json.dumps(manifest, indent=1).encode('utf-8'))
    return manifest


def read_manifest(bundle_dir):
    return json.loads((Path(bundle_dir) / 'manifest.json').read_text('utf-8'))


def _state(tree, rel):
    """Current identity of a path: None if absent, (sha256, mode) for a regular file,
    ('unsupported', 'unsupported') for anything else or anything unreachable safely."""
    try:
        st = os.lstat(Path(tree) / rel)
    except FileNotFoundError:
        return None
    except OSError:
        return ('unsupported', 'unsupported')
    if not stat.S_ISREG(st.st_mode):
        return ('unsupported', 'unsupported')
    try:
        return (paths.sha256_bytes(paths.read_regular(tree, rel)), paths.git_mode(st.st_mode))
    except (ProtocolError, OSError):
        return ('unsupported', 'unsupported')


def _before(d):
    return None if d['op'] == 'create' else (d['before_sha256'], d['before_mode'])


def _after(d):
    return None if d['op'] == 'delete' else (d['after_sha256'], d['after_mode'])


def check_before_hashes(tree, manifest):
    """Paths whose current (sha256, mode) != before (absent for create)."""
    return [d['path'] for d in manifest['changes'] if _state(tree, d['path']) != _before(d)]


def classify_publish(tree, manifest):
    out = {}
    for d in manifest['changes']:
        cur = _state(tree, d['path'])
        out[d['path']] = 'after' if cur == _after(d) else 'before' if cur == _before(d) else 'other'
    return out


def apply_bundle(bundle_dir, manifest, tree, only=None):
    """Apply in manifest order; the caller has verified before-hashes. Returns paths touched."""
    bundle_dir, tree = Path(bundle_dir), Path(tree)
    wanted = None if only is None else set(only)
    touched = []
    for d in manifest['changes']:
        path, op = d['path'], d['op']
        if wanted is not None and path not in wanted:
            continue
        paths.normalize_repo_path(path)
        if op in ('create', 'modify'):
            data = (bundle_dir / 'blobs' / d['after_sha256']).read_bytes()
            if paths.sha256_bytes(data) != d['after_sha256']:
                raise ProtocolError('bad_state', 'bundle blob corrupted: %s' % path, {'path': path})
            paths.atomic_write(tree, path, data, d['after_mode'])
        elif op == 'chmod':
            paths.set_exec_bit(tree, path, d['after_mode'])
        elif op == 'delete':
            paths.safe_unlink(tree, path)
        else:
            raise ProtocolError('bad_state', 'unknown bundle op %r for %s' % (op, path), {'path': path})
        touched.append(path)
    return touched
