"""Coordinator + worker operations; every mutation is one write_txn with typed refusals."""
from __future__ import annotations

import json
import os
import re
import shlex
import stat
from pathlib import Path

from . import bundle, db, gitx, model, paths, scope
from .db import ProtocolError
from .model import ACTIVE_STATES, CLAIMABLE_EXPIRED, TERMINAL_STATES

OWNER_STATES = frozenset({'claimed', 'running', 'blocked', 'conflicted', 'submitted'})
OWNER_NOTE = ("an owner prefixed 'orch:' or 'run:' is an orchestrated worker, not a human's terminal "
              "session, so `wcp ps` tells the two apart")
OWNER_RE = re.compile(r'\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')
_REGULAR = ('100644', '100755')


def validate_owner(owner: str) -> str:
    if not isinstance(owner, str) or not OWNER_RE.match(owner):
        raise ProtocolError('usage', 'invalid owner %r: must match %s' % (owner, OWNER_RE.pattern),
                            {'owner': owner})
    return owner


def link_dirs(conn) -> list[str]:
    return json.loads(db.meta_get(conn, 'link_dirs', '[]') or '[]')


def required_checks(conn) -> list[dict]:
    return json.loads(db.meta_get(conn, 'required_checks', '[]') or '[]')


def load_baseline(conn, worktree_path) -> dict[str, tuple[str, str]]:
    return {r['path']: (r['sha256'], r['mode']) for r in conn.execute(
        'SELECT path, sha256, mode FROM baseline_files WHERE worktree_path=?', (str(worktree_path),))}


def latest_submission(conn, task_id) -> model.Submission | None:
    row = conn.execute('SELECT * FROM submissions WHERE task_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1',
                       (task_id,)).fetchone()
    return None if row is None else model.Submission.from_row(row)


def receipts_for(conn, submission_id) -> list[dict]:
    return [dict(r, trust='untrusted_observation') for r in conn.execute(
        'SELECT * FROM receipts WHERE submission_id=? ORDER BY id', (submission_id,))]


def active_tasks(conn, at: float) -> list[model.Task]:
    return [model.Task.from_row(r) for r in conn.execute(
        'SELECT * FROM tasks WHERE state IN (%s) ORDER BY created_at, id' % ','.join('?' * len(ACTIVE_STATES)),
        sorted(ACTIVE_STATES))]


def _fresh_id(conn, prefix, table):
    while True:
        tid = db.new_id(prefix)
        if conn.execute('SELECT 1 FROM %s WHERE id=?' % table, (tid,)).fetchone() is None:
            return tid


def _scope_pattern(p: str) -> str:
    p = paths.normalize_repo_path(p)
    if paths.is_sensitive(p):
        raise ProtocolError('sensitive_path', 'credential-shaped scope pattern refused: %s' % p, {'path': p})
    return p


def _owner_gate(task: model.Task, owner: str, epoch, at: float) -> None:
    validate_owner(owner)
    if task.epoch is None or int(epoch) != task.epoch:
        raise ProtocolError('stale_epoch', 'task %s is at epoch %s, not %s' % (task.id, task.epoch, epoch),
                            {'task_id': task.id, 'epoch': task.epoch, 'given': epoch})
    if task.owner != owner:
        raise ProtocolError('not_owner', 'task %s is owned by %s' % (task.id, task.owner),
                            {'task_id': task.id, 'owner': task.owner})
    if task.lease_expired(at):
        raise ProtocolError('lease_expired', 'lease for %s expired; run wcp task recover %s' % (task.id, task.id),
                            {'task_id': task.id, 'lease_expires_at': task.lease_expires_at})


def _bump(conn, task: model.Task, expect_version, **updates) -> None:
    """Non-state update with the same version CAS as transition()."""
    if expect_version is not None and task.version != expect_version:
        raise ProtocolError('stale_version', 'task %s is at version %d, expected %d' % (
            task.id, task.version, expect_version), {'task_id': task.id})
    cols = ['version', 'updated_at'] + list(updates)
    vals = [task.version + 1, db.now()] + list(updates.values())
    res = conn.execute('UPDATE tasks SET %s WHERE id=? AND version=?' % ', '.join(c + '=?' for c in cols),
                       vals + [task.id, task.version])
    if res.rowcount != 1:
        raise ProtocolError('stale_version', 'task %s changed underneath' % task.id, {'task_id': task.id})


def _bad_state(task: model.Task, what: str) -> ProtocolError:
    return ProtocolError('bad_state', 'task %s is %s; %s' % (task.id, task.state, what),
                         {'task_id': task.id, 'state': task.state})


# ---------------------------------------------------------------- overlap arbiter

def assert_no_overlap(conn, task_id, path_patterns, resources, at: float, *,
                      include_ready: bool = False, against_tasks: bool = True) -> None:
    """The one overlap decision: preserve roots, then every other ACTIVE task (+ ready ones on demand)."""
    path_patterns, resources = list(path_patterns), list(resources)
    roots = [r.strip('/') for r in json.loads(db.meta_get(conn, 'preserve_roots', '[]') or '[]')]
    hits = scope.overlaps(path_patterns, [r + '/**' for r in roots if r and r != '.'])
    if hits:
        raise ProtocolError('preserve_root', 'scope overlaps a preserve root: ' + ', '.join(a for a, _ in hits),
                            {'task_id': task_id, 'pairs': [list(h) for h in hits]})
    if not against_tasks:
        return
    states = sorted(set(ACTIVE_STATES) | ({'ready'} if include_ready else set()))
    for row in conn.execute('SELECT id FROM tasks WHERE id != ? AND state IN (%s) ORDER BY created_at, id'
                            % ','.join('?' * len(states)), [task_id] + states):
        other = model.load_scopes(conn, row['id'])
        pairs = scope.overlaps(path_patterns, other['path'])
        res = scope.resource_overlaps(resources, other['resource'])
        if pairs or res:
            raise ProtocolError('scope_overlap', 'task %s overlaps active task %s: %s' % (
                task_id, row['id'], ', '.join(['%s ~ %s' % p for p in pairs] + res)),
                {'task_id': task_id, 'other': row['id'], 'paths': [list(p) for p in pairs], 'resources': res})


# ---------------------------------------------------------------- coordinator

def create_task_in_txn(conn, goal, write=(), resource=(), depends=(), checks=()) -> model.Task:
    """The caller holds write_txn (a `wcp run` creates its whole graph in one); a refusal rolls it all back."""
    if not conn.in_transaction:
        raise RuntimeError('create_task_in_txn requires write_txn')
    write = [_scope_pattern(p) for p in write]
    resource = [str(r) for r in resource]
    tid = _fresh_id(conn, 't-', 'tasks')
    at = db.now()
    assert_no_overlap(conn, tid, write, resource, at, against_tasks=False)
    conn.execute("INSERT INTO tasks(id, goal, state, version, created_at, updated_at) VALUES (?,?,'proposed',0,?,?)",
                 (tid, goal, at, at))
    conn.executemany('INSERT OR IGNORE INTO task_scopes(task_id, kind, pattern) VALUES (?,?,?)',
                     [(tid, 'path', p) for p in write] + [(tid, 'resource', r) for r in resource])
    conn.executemany('INSERT OR IGNORE INTO task_deps(task_id, depends_on) VALUES (?,?)',
                     [(tid, d) for d in depends])
    conn.executemany('INSERT INTO task_checks(task_id, ord, cmd) VALUES (?,?,?)',
                     [(tid, i, c) for i, c in enumerate(checks)])
    model.load_checks(conn, tid)
    db.add_event(conn, tid, 'create', {'goal': goal, 'write': write, 'resource': resource,
                                       'depends': list(depends), 'checks': list(checks)})
    return model.load_task(conn, tid)


def create_task(conn, goal, write=(), resource=(), depends=(), checks=()) -> model.Task:
    with db.write_txn(conn):
        return create_task_in_txn(conn, goal, write, resource, depends, checks)


def waiting_on(conn, task_id, at: float) -> list[str]:
    """Active tasks (lease state irrelevant) whose scope overlaps this one, plus unaccepted dependencies."""
    sc = model.load_scopes(conn, task_id)
    out = []
    for row in conn.execute('SELECT id FROM tasks WHERE id != ? AND state IN (%s) ORDER BY created_at, id'
                            % ','.join('?' * len(ACTIVE_STATES)), [task_id] + sorted(ACTIVE_STATES)):
        other = model.load_scopes(conn, row['id'])
        if scope.overlaps(sc['path'], other['path']) or scope.resource_overlaps(sc['resource'], other['resource']):
            out.append(row['id'])
    for d in model.load_deps(conn, task_id):
        if d not in out and not _is_accepted(conn, d):
            out.append(d)
    return out


def ready_task(conn, task_id) -> model.Task:
    """Admit: preserve roots and dependency existence are gates; overlap is reported by waiting_on(), and
    serialized at claim (addendum A1)."""
    with db.write_txn(conn):
        task = model.load_task(conn, task_id)
        if task.state != 'proposed':
            raise _bad_state(task, 'only proposed tasks can be admitted')
        missing = [d for d in model.load_deps(conn, task_id)
                   if conn.execute('SELECT 1 FROM tasks WHERE id=?', (d,)).fetchone() is None]
        if missing:
            raise ProtocolError('not_found', 'unknown dependencies: ' + ', '.join(missing),
                                {'task_id': task_id, 'depends': missing})
        sc = model.load_scopes(conn, task_id)
        assert_no_overlap(conn, task_id, sc['path'], sc['resource'], db.now(), against_tasks=False)
        return model.transition(conn, task, 'ready')


def cancel(conn, task_id) -> model.Task:
    with db.write_txn(conn):
        return model.transition(conn, model.load_task(conn, task_id), 'cancelled')


def release(conn, task_id) -> model.Task:
    """Force-expire the lease under a new epoch; the old owner's epoch is fenced."""
    with db.write_txn(conn):
        task = model.load_task(conn, task_id)
        if task.state not in OWNER_STATES:
            raise _bad_state(task, 'only claimed/running/blocked/conflicted/submitted tasks hold a releasable lease')
        epoch = db.next_epoch(conn)
        _bump(conn, task, None, epoch=epoch, lease_expires_at=db.now())
        db.add_event(conn, task_id, 'release', {'old_epoch': task.epoch, 'epoch': epoch, 'owner': task.owner})
        return model.load_task(conn, task_id)


# ---------------------------------------------------------------- claim / provisioning

def _worktree_row(conn, path) -> model.Worktree | None:
    if not path:
        return None
    row = conn.execute('SELECT * FROM worktrees WHERE path=?', (str(path),)).fetchone()
    return None if row is None else model.Worktree.from_row(row)


def _under(parent: Path, child) -> bool:
    p, c = os.path.realpath(parent), os.path.realpath(child)
    return c != p and os.path.commonpath([p, c]) == p


def _prove_scratch(root, task: model.Task, row: model.Worktree, wt_dir: Path) -> None:
    """In-txn proof that a crashed provisioning scratch may be force-removed (spec "Worktree provisioning")."""
    reg = gitx.registration(root, Path(row.path))
    ok = (row.status == 'provisioning' and row.task_id == task.id and row.epoch == task.epoch
          and row.path == task.worktree_path and _under(wt_dir, row.path) and reg in ('active', 'absent'))
    if not ok:
        raise ProtocolError('bad_state', 'provisioning scratch %s cannot be proven removable (registration %s); '
                            'inspect it by hand' % (row.path, reg),
                            {'task_id': task.id, 'worktree': row.path, 'registration': reg})


def _transfer(conn, old: model.Worktree, new_path: Path, new_baseline: dict, links: list[str]):
    """Copy the retired worktree's delta (vs its own baseline) into the new worktree, only for paths whose
    baseline hash did not move; everything else is rebase_needed and stays in the retired worktree."""
    old_path = Path(old.path)
    recovered, rebase = [], []
    if not old_path.is_dir():
        return recovered, rebase
    old_baseline = load_baseline(conn, old.path)
    try:
        old_current = gitx.list_entries(old_path, links)
    except gitx.GitError:
        return recovered, rebase  # ponytail: a hand-broken retired worktree transfers nothing, silently

    def regular(e):
        return e is None or e[1] in _REGULAR

    for rel in sorted(set(old_baseline) | set(old_current)):
        ob, oc = old_baseline.get(rel), old_current.get(rel)
        if ob == oc:
            continue
        nb = new_baseline.get(rel)
        old_hash, new_hash = (ob[0] if ob else None), (nb[0] if nb else None)
        entry = {'path': rel, 'retired_worktree': old.path, 'old_baseline': old_hash, 'new_baseline': new_hash}
        if old_hash != new_hash or not (regular(ob) and regular(oc) and regular(nb)):
            rebase.append(entry)
            continue
        if oc is None:
            if nb is not None:
                paths.safe_unlink(new_path, rel)
            recovered.append({'path': rel, 'op': 'delete', 'sha256': None, 'mode': None})
            continue
        data = paths.read_regular(old_path, rel)
        if paths.sha256_bytes(data) != oc[0]:  # the old process is still writing it
            entry['reason'] = 'unstable'
            rebase.append(entry)
            continue
        paths.atomic_write(new_path, rel, data, oc[1])
        if paths.sha256_bytes(paths.read_regular(new_path, rel)) != oc[0]:
            raise ProtocolError('bad_state', 'transfer verification failed for %s' % rel, {'path': rel})
        op = 'create' if ob is None else ('chmod' if ob[0] == oc[0] else 'modify')
        recovered.append({'path': rel, 'op': op, 'sha256': oc[0], 'mode': oc[1]})
    return recovered, rebase


def _lease_live(task: model.Task, at: float) -> ProtocolError:
    return ProtocolError('bad_state', 'task %s is %s by %s; lease expires in %ds' % (
        task.id, task.state, task.owner, int(task.lease_expires_at - at)),
        {'task_id': task.id, 'state': task.state, 'owner': task.owner, 'lease_expires_at': task.lease_expires_at})


def _claim_gate(task: model.Task, at: float) -> None:
    """A fresh claim, or anyone's reclaim of an expired lease."""
    if task.state == 'ready' or (task.state in CLAIMABLE_EXPIRED and task.lease_expired(at)):
        return
    if task.state in CLAIMABLE_EXPIRED:
        raise _lease_live(task, at)
    raise _bad_state(task, 'not claimable')


def _same_run(task_owner, owner: str) -> bool:
    """`run:<run_id>:<key>` owners belong to one run; a plain owner string only matches itself."""
    if task_owner == owner:
        return True
    prefix, sep, _ = owner.rpartition(':')
    return bool(sep) and task_owner is not None and task_owner.startswith(prefix + ':')


def _untransferred_retired(conn, task_id) -> model.Worktree | None:
    """The worktree a crashed or failed re-provision retired in its step 0 without reaching step 5: retired,
    never the `from` of a recovered/rebase_needed event, not a removed scratch, not cleaned up. Newest first."""
    done = set()
    for row in conn.execute("SELECT detail_json FROM events WHERE task_id=? AND kind IN "
                            "('recovered','rebase_needed','scratch_removed','cleanup')", (task_id,)):
        d = json.loads(row['detail_json'])
        done.add(d.get('from') or d.get('worktree'))
    for w in sorted(model.load_worktrees(conn, task_id), key=lambda w: w.epoch, reverse=True):
        if w.status == 'retired' and w.path not in done:
            return w
    return None


def _claim(conn, root, task_id, owner, lease_s, gate, *, activate='claimed', kind='claim') -> dict:
    """Steps 0-5 of the spec's provisioning for claim, reclaim, rebase and runner_reclaim alike.

    `gate(task, at)` runs first inside the step-0 transaction and decides admissibility; the deps and
    overlap checks, the epoch, the retire-and-transfer, and the obligations are the same for all."""
    validate_owner(owner)
    root = Path(root)
    links = link_dirs(conn)
    wt_dir = db.repo_dir(root) / 'worktrees'
    db.ensure_private(wt_dir, True)
    warnings: list[str] = []
    # step 0: every gate, the epoch, and the durable provisioning record in one transaction
    with db.write_txn(conn):
        task = model.load_task(conn, task_id)
        at = db.now()
        gate(task, at)
        unmet = [d for d in model.load_deps(conn, task_id) if not _is_accepted(conn, d)]
        if unmet:
            raise ProtocolError('deps_unmet', 'dependencies not accepted: ' + ', '.join(unmet),
                                {'task_id': task_id, 'depends': unmet})
        sc = model.load_scopes(conn, task_id)
        assert_no_overlap(conn, task_id, sc['path'], sc['resource'], at)
        epoch = db.next_epoch(conn)
        new_path = wt_dir / ('%s-e%d' % (task_id, epoch))
        old = _worktree_row(conn, task.worktree_path)
        transfer_from = scratch = None
        if old is not None and old.status == 'active':
            conn.execute("UPDATE worktrees SET status='retired' WHERE path=?", (old.path,))
            db.add_event(conn, task_id, 'retired', {'worktree': old.path, 'epoch': old.epoch,
                                                    'previous_owner': task.owner, 'new_epoch': epoch, 'by': owner,
                                                    'kind': kind})
            transfer_from = old
        else:
            if old is not None and old.status == 'provisioning':
                _prove_scratch(root, task, old, wt_dir)
                conn.execute("UPDATE worktrees SET status='retired' WHERE path=?", (old.path,))
                db.add_event(conn, task_id, 'scratch_removed', {'worktree': old.path, 'epoch': old.epoch})
                scratch = old.path
            # a re-provision that crashed or failed between its step 0 and step 5 already retired the worktree
            # holding the delta; it is transferred now, so the work is neither dropped nor forgotten (A4)
            transfer_from = _untransferred_retired(conn, task_id)
        lease_expires_at = at + lease_s
        task = model.transition(conn, task, 'provisioning', owner=owner, epoch=epoch,
                                lease_expires_at=lease_expires_at, worktree_path=str(new_path),
                                base_revision=None, blocked_reason=None)
        conn.execute("INSERT INTO worktrees(path, task_id, epoch, status, created_at) VALUES (?,?,?,'provisioning',?)",
                     (str(new_path), task_id, epoch, at))
    # filesystem work: steps 1-4 plus the reclaim transfer, none of it visible to a builder yet
    if scratch and not gitx.remove_worktree(root, Path(scratch), force=True):
        warnings.append('provisioning scratch %s could not be removed; inspect it by hand' % scratch)
    recovered: list = []
    rebase: list = []
    try:
        info = gitx.provision_worktree(root, new_path, links)
        new_baseline = gitx.list_entries(new_path, links)
        if transfer_from is not None:
            recovered, rebase = _transfer(conn, transfer_from, new_path, new_baseline, links)
    except BaseException:
        _provisioning_failed(conn, root, task_id, epoch, new_path)
        raise
    # step 5: baseline + activation; only now is the path handed out
    try:
        with db.write_txn(conn):
            cur = model.load_task(conn, task_id)
            if cur.state != 'provisioning' or cur.epoch != epoch or cur.worktree_path != str(new_path):
                raise ProtocolError('stale_epoch', 'task %s was reclaimed or released during provisioning' % task_id,
                                    {'task_id': task_id, 'epoch': epoch, 'state': cur.state})
            conn.executemany('INSERT OR REPLACE INTO baseline_files(worktree_path, path, sha256, mode) VALUES (?,?,?,?)',
                             [(str(new_path), p, h, m) for p, (h, m) in new_baseline.items()])
            conn.execute("UPDATE worktrees SET status='active' WHERE path=?", (str(new_path),))
            if transfer_from is not None:  # always, even empty: it marks the retired worktree as transferred
                db.add_event(conn, task_id, 'recovered', {'from': transfer_from.path, 'to': str(new_path),
                                                          'paths': recovered})
            if rebase:
                db.add_event(conn, task_id, 'rebase_needed', {'from': transfer_from.path, 'to': str(new_path),
                                                              'paths': rebase})
                # durable obligation per moved path: submit refuses rebase_pending until each is re-applied or kept
                conn.executemany("INSERT INTO rebase_obligations(task_id, epoch, path, status, retired_worktree) "
                                 "VALUES (?,?,?,'pending',?)",
                                 [(task_id, epoch, e['path'], e['retired_worktree']) for e in rebase])
            cur = model.transition(conn, cur, 'claimed', base_revision=info['head'])
            if activate == 'running':
                cur = model.transition(conn, cur, 'running')
    except ProtocolError as e:
        if e.code == 'stale_epoch':
            gitx.remove_worktree(root, new_path, force=True)  # our own scratch, never handed to a builder
        raise
    if info.get('sensitive_skipped'):
        warnings.append('%d credential-shaped untracked path(s) were not copied into the worktree'
                        % info['sensitive_skipped'])
    return {'task': cur, 'worktree': str(new_path), 'epoch': epoch, 'lease_expires_at': lease_expires_at,
            'base_revision': info['head'], 'recovered': recovered, 'rebase_needed': rebase,
            'retired_worktree': transfer_from.path if transfer_from is not None else None,
            'warnings': warnings, 'codex_hint': 'codex -C %s -s workspace-write' % shlex.quote(str(new_path)),
            'owner_note': OWNER_NOTE}


def claim_task(conn, root, task_id, owner, lease_s=900, wait_s: float = 0) -> dict:
    """wait_s > 0 (or -1: no limit) polls db.sleep(2) around the unchanged atomic claim while it refuses
    scope_overlap / deps_unmet; every other refusal is immediate. Timeout -> waiting_timeout."""
    deadline = None if wait_s < 0 else db.now() + wait_s
    while True:
        try:
            return _claim(conn, root, task_id, owner, lease_s, _claim_gate)
        except ProtocolError as e:
            if e.code not in ('scope_overlap', 'deps_unmet'):
                raise
            e.detail['waiting_on'] = waiting_on(conn, task_id, db.now())
            if deadline is not None and db.now() >= deadline:
                if wait_s == 0:
                    raise
                raise ProtocolError('waiting_timeout', 'task %s still waiting on %s after %ss' % (
                    task_id, ', '.join(e.detail['waiting_on']) or e.code, wait_s),
                    {'task_id': task_id, 'waiting_on': e.detail['waiting_on'], 'waited_s': wait_s})
            db.sleep(2)


def rebase_task(conn, root, task_id, owner, epoch, lease_s=900) -> dict:
    """The owner's own reclaim (addendum A4): fresh worktree from current canonical under a new epoch, the
    unchanged-baseline delta transferred, an obligation per moved path, state running. A prior submission
    is superseded by the epoch."""
    def gate(task, at):
        if task.state not in ('running', 'conflicted', 'submitted'):
            raise _bad_state(task, 'only running/conflicted/submitted tasks rebase')
        _owner_gate(task, owner, epoch, at)
    return _claim(conn, root, task_id, owner, lease_s, gate, activate='running', kind='rebase')


def runner_reclaim(conn, root, task_id, owner, lease_s=900) -> dict:
    """A run's reclaim of its own task (addendum B6): like a reclaim on an expired lease, keeping the owner
    string; also proceeds on a LIVE lease when the task's owner is this run's own — the caller holds
    runner.lock, which proves the runner that took that lease is gone."""
    def gate(task, at):
        if task.state not in CLAIMABLE_EXPIRED:
            raise _bad_state(task, 'not reclaimable')
        if not task.lease_expired(at) and not _same_run(task.owner, owner):
            raise _lease_live(task, at)
    return _claim(conn, root, task_id, owner, lease_s, gate, kind='runner_reclaim')


def runner_heartbeat(conn, task_id, owner, lease_s=900) -> model.Task:
    """A run's lease renewal for its own task (addendum B4/B6): lease only, no state change, and legal on an
    expired lease — the caller holds runner.lock, so an owner string of this run is provably its own and a
    lapsed lease only means the previous runner died or a verify outlasted it."""
    validate_owner(owner)
    with db.write_txn(conn):
        task = model.load_task(conn, task_id)
        if task.owner != owner or task.epoch is None:
            raise ProtocolError('not_owner', 'task %s is owned by %s' % (task.id, task.owner),
                                {'task_id': task.id, 'owner': task.owner})
        if task.state not in OWNER_STATES:
            raise _bad_state(task, 'no lease to renew')
        lease = db.now() + lease_s
        _bump(conn, task, None, lease_expires_at=lease)
        db.add_event(conn, task_id, 'heartbeat', {'lease_expires_at': lease, 'epoch': task.epoch, 'runner': True})
        return model.load_task(conn, task_id)


def ack_obligation(conn, task_id, owner, epoch, path) -> None:
    """Owner's explicit "canonical's version stands" for one pending rebase obligation -> kept."""
    rel = paths.normalize_repo_path(path)
    with db.write_txn(conn):
        task = model.load_task(conn, task_id)
        _owner_gate(task, owner, epoch, db.now())
        res = conn.execute("UPDATE rebase_obligations SET status='kept' WHERE task_id=? AND epoch=? AND path=? "
                           "AND status='pending'", (task_id, task.epoch, rel))
        if res.rowcount != 1:
            raise ProtocolError('not_found', 'no pending rebase obligation for %s at epoch %s on %s' % (
                rel, task.epoch, task_id), {'task_id': task_id, 'epoch': task.epoch, 'path': rel})
        db.add_event(conn, task_id, 'obligation_kept', {'epoch': task.epoch, 'path': rel})


def _is_accepted(conn, task_id) -> bool:
    row = conn.execute('SELECT state FROM tasks WHERE id=?', (task_id,)).fetchone()
    return row is not None and row['state'] == 'accepted'


def _provisioning_failed(conn, root, task_id, epoch, new_path: Path) -> None:
    removed = gitx.remove_worktree(root, new_path, force=True)  # scratch this process created; never exposed
    try:
        with db.write_txn(conn):
            cur = model.load_task(conn, task_id)
            if cur.state == 'provisioning' and cur.epoch == epoch and cur.worktree_path == str(new_path):
                if removed:
                    conn.execute('DELETE FROM baseline_files WHERE worktree_path=?', (str(new_path),))
                    conn.execute('DELETE FROM worktrees WHERE path=?', (str(new_path),))
                # else: the directory is still there, so its row stays 'provisioning' and keeps the
                # path attributable (the hook denies writes to it; cleanup can still refuse on it)
                model.transition(conn, cur, 'ready', owner=None, epoch=None, lease_expires_at=None,
                                 worktree_path=None, base_revision=None)
                db.add_event(conn, task_id, 'provisioning_failed', {'worktree': str(new_path), 'epoch': epoch,
                                                                    'removed': removed})
    except Exception:
        pass  # the original failure is what the caller sees; a later claim re-provisions from the durable record


# ---------------------------------------------------------------- worker

def heartbeat(conn, task_id, owner, epoch, lease_s=900, expect_version=None) -> model.Task:
    with db.write_txn(conn):
        task = model.load_task(conn, task_id)
        at = db.now()
        _owner_gate(task, owner, epoch, at)
        if task.state not in OWNER_STATES:
            raise _bad_state(task, 'no lease to renew')
        lease = at + lease_s
        if task.state in ('claimed', 'blocked', 'conflicted'):
            return model.transition(conn, task, 'running', expect_version=expect_version,
                                    lease_expires_at=lease, blocked_reason=None)
        _bump(conn, task, expect_version, lease_expires_at=lease)
        db.add_event(conn, task_id, 'heartbeat', {'lease_expires_at': lease, 'epoch': task.epoch})
        return model.load_task(conn, task_id)


def block(conn, task_id, owner, epoch, reason, expect_version=None) -> model.Task:
    with db.write_txn(conn):
        task = model.load_task(conn, task_id)
        _owner_gate(task, owner, epoch, db.now())
        return model.transition(conn, task, 'blocked', expect_version=expect_version, blocked_reason=reason)


def abandon(conn, task_id, owner, epoch, expect_version=None) -> model.Task:
    with db.write_txn(conn):
        task = model.load_task(conn, task_id)
        _owner_gate(task, owner, epoch, db.now())
        return model.transition(conn, task, 'abandoned', expect_version=expect_version)


def submit(conn, root, task_id, owner, epoch, record_failing=False, expect_version=None) -> dict:
    root = Path(root)
    links = link_dirs(conn)
    task = model.load_task(conn, task_id)
    _owner_gate(task, owner, epoch, db.now())  # 1. owner/epoch/lease
    if task.state not in ('claimed', 'running'):
        raise _bad_state(task, 'only claimed/running tasks can submit')
    wt = _worktree_row(conn, task.worktree_path)
    if wt is None or wt.status != 'active':
        raise _bad_state(task, 'no active worktree')
    tree = Path(wt.path)
    changes = bundle.diff_against_baseline(tree, load_baseline(conn, wt.path), links)
    changed = {c.path for c in changes}
    pending = [o for o in model.load_obligations(conn, task_id, task.epoch) if o.status == 'pending']
    unresolved = [o.path for o in pending if o.path not in changed]  # untouched since the rebase: not forgotten
    if unresolved:
        raise ProtocolError('rebase_pending', 'canonical moved under %s before the rebase; re-apply each against '
                            'the new baseline in %s or run wcp task ack %s --owner %s --epoch %s --keep PATH; the '
                            'previous content is retained in %s' % (', '.join(unresolved), tree, task_id, owner,
                                                                    epoch, pending[0].retired_worktree),
                            {'task_id': task_id, 'epoch': task.epoch, 'paths': unresolved,
                             'retired_worktree': pending[0].retired_worktree})
    if not changes:
        raise ProtocolError('bad_state', 'nothing changed against the baseline in %s' % tree,
                            {'task_id': task_id, 'worktree': wt.path})
    sensitive = [c.path for c in changes if paths.is_sensitive(c.path)]
    if sensitive:
        raise ProtocolError('sensitive_path', 'credential-shaped paths cannot be bundled: ' + ', '.join(sensitive),
                            {'paths': sensitive})
    sc = model.load_scopes(conn, task_id)  # 2. scope; nothing stored on failure
    bad = [c.path for c in changes if not scope.matches(sc['path'], c.path)]
    if bad:
        raise ProtocolError('out_of_scope', 'paths outside scope %s: %s' % (sc['path'], ', '.join(bad)),
                            {'task_id': task_id, 'paths': bad, 'scope': sc['path']})
    receipts = [gitx.run_check(c, tree) for c in model.load_checks(conn, task_id)]  # 3. acceptance checks
    failed = [r for r in receipts if r['exit_code'] != 0]
    sub_id = _fresh_id(conn, 's-', 'submissions')
    store = not failed or record_failing
    manifest = bdir = None
    if store:
        bdir = db.repo_dir(root) / 'bundles' / task_id / sub_id
        manifest = bundle.write_bundle(bdir, tree, changes)
    with db.write_txn(conn):
        cur = model.load_task(conn, task_id)
        at = db.now()
        _owner_gate(cur, owner, epoch, at)  # epoch/owner/lease re-gated; the state is re-gated by transition()
        if store:
            conn.execute('INSERT INTO submissions(id, task_id, epoch, worktree_path, created_at, bundle_dir, manifest_json) '
                         'VALUES (?,?,?,?,?,?,?)',
                         (sub_id, task_id, cur.epoch, wt.path, at, str(bdir), json.dumps(manifest, sort_keys=True)))
        conn.executemany('INSERT INTO receipts(task_id, submission_id, phase, cmd, exit_code, duration_ms, output_tail, '
                         'created_at) VALUES (?,?,?,?,?,?,?,?)',
                         [(task_id, sub_id if store else None, 'submit', r['cmd'], r['exit_code'], r['duration_ms'],
                           r['output_tail'], at) for r in receipts])
        if failed:
            if cur.state == 'claimed':
                model.transition(conn, cur, 'running', expect_version=expect_version)
            db.add_event(conn, task_id, 'checks_failed', {'phase': 'submit', 'submission': sub_id if store else None,
                                                          'failed': [r['cmd'] for r in failed]})
            state = 'running'
        else:
            model.transition(conn, cur, 'submitted', expect_version=expect_version)
            if pending:  # every pending path is in `changes` (gated above): the builder re-applied it
                conn.execute("UPDATE rebase_obligations SET status='resolved' WHERE task_id=? AND epoch=? "
                             "AND status='pending'", (task_id, cur.epoch))
            db.add_event(conn, task_id, 'submit', {'submission': sub_id, 'worktree': wt.path,
                                                   'changes': [c.path for c in changes],
                                                   'obligations_resolved': [o.path for o in pending]})
            state = 'submitted'
    if failed:
        raise ProtocolError('checks_failed', '%d of %d submit checks failed; task stays running' % (
            len(failed), len(receipts)),
            {'task_id': task_id, 'submission_id': sub_id if store else None, 'receipts': receipts, 'state': state})
    return {'submission_id': sub_id, 'changes': manifest['changes'], 'receipts': receipts, 'state': state,
            'worktree': wt.path}


# ---------------------------------------------------------------- scope requests

def _load_request(conn, task_id, req_id) -> model.ScopeRequest:
    row = conn.execute('SELECT * FROM scope_requests WHERE id=? AND task_id=?', (int(req_id), task_id)).fetchone()
    if row is None:
        raise ProtocolError('not_found', 'no scope request %s on task %s' % (req_id, task_id),
                            {'task_id': task_id, 'request': req_id})
    return model.ScopeRequest.from_row(row)


def request_scope(conn, task_id, owner, epoch, kind, pattern, reason) -> int:
    if kind not in ('path', 'resource'):
        raise ProtocolError('bad_state', 'scope kind must be path or resource', {'kind': kind})
    if kind == 'path':
        pattern = _scope_pattern(pattern)
    with db.write_txn(conn):
        task = model.load_task(conn, task_id)
        _owner_gate(task, owner, epoch, db.now())
        if task.state not in OWNER_STATES:
            raise _bad_state(task, 'cannot request scope')
        cur = conn.execute("INSERT INTO scope_requests(task_id, kind, pattern, reason, status, created_at) "
                           "VALUES (?,?,?,?,'pending',?)", (task_id, kind, pattern, reason, db.now()))
        db.add_event(conn, task_id, 'scope_request', {'request': cur.lastrowid, 'kind': kind,
                                                      'pattern': pattern, 'reason': reason})
        return cur.lastrowid


def approve_scope(conn, task_id, req_id) -> model.ScopeRequest:
    with db.write_txn(conn):
        task = model.load_task(conn, task_id)
        if task.state in ('verifying', 'publishing') or task.state in TERMINAL_STATES:
            raise _bad_state(task, 'scope is approved only while the task is live and not integrator-owned')
        req = _load_request(conn, task_id, req_id)
        if req.status != 'pending':
            raise ProtocolError('bad_state', 'scope request %s is %s' % (req.id, req.status),
                                {'task_id': task_id, 'request': req.id, 'status': req.status})
        sc = model.load_scopes(conn, task_id)
        new_paths = sc['path'] + ([req.pattern] if req.kind == 'path' else [])
        new_res = sc['resource'] + ([req.pattern] if req.kind == 'resource' else [])
        assert_no_overlap(conn, task_id, new_paths, new_res, db.now(), include_ready=(task.state == 'ready'))
        conn.execute('INSERT OR IGNORE INTO task_scopes(task_id, kind, pattern) VALUES (?,?,?)',
                     (task_id, req.kind, req.pattern))
        conn.execute("UPDATE scope_requests SET status='approved' WHERE id=?", (req.id,))
        db.add_event(conn, task_id, 'scope_approved', {'request': req.id, 'kind': req.kind, 'pattern': req.pattern})
        return _load_request(conn, task_id, req_id)


def deny_scope(conn, task_id, req_id, reason) -> model.ScopeRequest:
    with db.write_txn(conn):
        model.load_task(conn, task_id)
        req = _load_request(conn, task_id, req_id)
        if req.status != 'pending':
            raise ProtocolError('bad_state', 'scope request %s is %s' % (req.id, req.status),
                                {'task_id': task_id, 'request': req.id, 'status': req.status})
        conn.execute("UPDATE scope_requests SET status='denied' WHERE id=?", (req.id,))
        db.add_event(conn, task_id, 'scope_denied', {'request': req.id, 'kind': req.kind,
                                                     'pattern': req.pattern, 'reason': reason})
        return _load_request(conn, task_id, req_id)


# ---------------------------------------------------------------- cleanup

def _accounted(conn, task_id, wt: model.Worktree) -> dict[str, set]:
    """This worktree's own baseline, plus the ACCEPTED bundle's after-state (or, for a retired
    worktree, what was transferred out of it). A rejected or superseded submission is not
    authorization to delete anything: its content stays unaccounted."""
    acc = {p: {v} for p, v in load_baseline(conn, wt.path).items()}
    row = conn.execute("SELECT detail_json FROM events WHERE task_id=? AND kind='publish_finished' "
                       "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if row is not None:
        sub = conn.execute('SELECT manifest_json FROM submissions WHERE id=? AND worktree_path=?',
                           (json.loads(row['detail_json']).get('submission'), wt.path)).fetchone()
        for d in (json.loads(sub['manifest_json'])['changes'] if sub is not None else ()):
            if d['after_sha256']:
                acc.setdefault(d['path'], set()).add((d['after_sha256'], d['after_mode']))
    for row in conn.execute("SELECT detail_json FROM events WHERE task_id=? AND kind='recovered'", (task_id,)):
        detail = json.loads(row['detail_json'])
        if detail.get('from') == wt.path:
            for e in detail.get('paths', []):
                if e.get('sha256'):
                    acc.setdefault(e['path'], set()).add((e['sha256'], e['mode']))
    return acc


def unaccounted_entries(conn, task_id, wt: model.Worktree, links: list[str]) -> list[str]:
    """lstat inventory of EVERY entry (ignored ones included); only .git and the named link_dirs are exempt."""
    tree = Path(wt.path)
    acc = _accounted(conn, task_id, wt)
    linkset = {paths.normalize_repo_path(d) for d in links}
    bad: list[str] = []

    def scan(dirpath, prefix):
        for entry in sorted(os.scandir(dirpath), key=lambda e: e.name):
            rel = entry.name if not prefix else prefix + '/' + entry.name
            if rel == '.git':
                continue
            st = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                scan(entry.path, rel)
                continue
            if stat.S_ISLNK(st.st_mode) and rel in linkset:
                continue
            if paths.is_sensitive(rel):
                bad.append(rel)  # never read; never accounted
                continue
            try:
                if stat.S_ISREG(st.st_mode):
                    ident = (paths.sha256_bytes(paths.read_regular(tree, rel)), paths.git_mode(st.st_mode))
                elif stat.S_ISLNK(st.st_mode):
                    ident = ('symlink:' + paths.sha256_bytes(os.readlink(os.fsencode(entry.path))), 'symlink')
                else:
                    ident = None
            except (ProtocolError, OSError):
                ident = None
            if ident is None or ident not in acc.get(rel, ()):
                bad.append(rel)

    scan(tree, '')
    return sorted(bad)


def cleanup(conn, root, task_id) -> list[Path]:
    root = Path(root)
    links = link_dirs(conn)
    task = model.load_task(conn, task_id)
    rows = model.load_worktrees(conn, task_id)
    wanted = ('active', 'retired') if task.state in TERMINAL_STATES else ('retired',)
    targets = [w for w in rows if w.status in wanted and os.path.lexists(w.path)]
    if not targets and task.state not in TERMINAL_STATES:
        raise _bad_state(task, 'only accepted/abandoned/cancelled tasks or retired worktrees can be cleaned up')
    removed = []
    for w in targets:
        bad = unaccounted_entries(conn, task_id, w, links)
        if bad:
            raise ProtocolError('unclean_worktree', 'worktree %s holds %d unaccounted entries: %s' % (
                w.path, len(bad), ', '.join(bad)), {'task_id': task_id, 'worktree': w.path, 'paths': bad})
        reg = gitx.registration(root, Path(w.path))
        # force is justified by the inventory above: every entry is baseline, bundle, or transferred content
        # ponytail: no lock between the inventory and the removal; a builder still writing there is a race
        if reg != 'active' or not gitx.remove_worktree(root, Path(w.path), force=True):
            raise ProtocolError('bad_state', 'worktree %s was not removed (registration %s); inspect it by hand'
                                % (w.path, reg), {'task_id': task_id, 'worktree': w.path, 'registration': reg})
        with db.write_txn(conn):
            conn.execute("UPDATE worktrees SET status='retired' WHERE path=?", (w.path,))
            db.add_event(conn, task_id, 'cleanup', {'worktree': w.path})
        removed.append(Path(w.path))
    return removed
