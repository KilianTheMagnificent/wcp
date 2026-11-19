"""Durable state -> the single next safe action."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import bundle, gitx, model, tasks
from .db import ProtocolError


def _minutes(seconds: float) -> str:
    m = int(abs(seconds) // 60)
    return '%d min' % m if m else '%d s' % int(abs(seconds))


def recover(conn, root, task_id, at: float) -> dict:
    task = model.load_task(conn, task_id)
    links = tasks.link_dirs(conn)
    if task.lease_expires_at is None:
        lease = 'none'
    elif task.lease_expired(at):
        lease = 'expired %s ago' % _minutes(at - task.lease_expires_at)
    else:
        lease = 'active, expires in %s' % _minutes(task.lease_expires_at - at)
    wt = task.worktree_path
    exists = bool(wt) and os.path.isdir(wt)
    delta = None
    delta_error = None
    if exists:
        try:
            delta = len(bundle.diff_against_baseline(Path(wt), tasks.load_baseline(conn, wt), links))
        except (ProtocolError, gitx.GitError) as e:
            delta_error = str(e)
    sub = tasks.latest_submission(conn, task_id)
    submission = None
    if sub is not None:
        submission = {'id': sub.id, 'epoch': sub.epoch, 'worktree': sub.worktree_path, 'created_at': sub.created_at,
                      'current_epoch': sub.epoch == task.epoch,
                      'changes': [d['path'] for d in sub.manifest()['changes']],
                      'receipts': tasks.receipts_for(conn, sub.id)}
    blockers = []
    if task.blocked_reason:
        blockers.append(task.blocked_reason)
    unmet = [d for d in model.load_deps(conn, task_id) if not tasks._is_accepted(conn, d)]
    if unmet:
        blockers.append('dependencies not accepted: ' + ', '.join(unmet))
    for row in conn.execute("SELECT id, kind, pattern FROM scope_requests WHERE task_id=? AND status='pending'",
                            (task_id,)):
        blockers.append('scope request %d pending (%s %s)' % (row['id'], row['kind'], row['pattern']))
    row = conn.execute("SELECT detail_json FROM events WHERE task_id=? AND kind='rebase_needed' ORDER BY id DESC LIMIT 1",
                       (task_id,)).fetchone()
    rebase = []
    if row is not None:
        detail = json.loads(row['detail_json'])
        if detail.get('to') == wt:
            rebase = detail.get('paths', [])
            blockers.append('rebase_needed: %s (stale content kept in %s)' % (
                ', '.join(e['path'] for e in rebase), detail.get('from')))
    worktrees = [{'path': w.path, 'epoch': w.epoch, 'status': w.status, 'exists': os.path.isdir(w.path)}
                 for w in model.load_worktrees(conn, task_id)]
    delta_text = ('its %d changed file(s)' % delta) if delta is not None else 'its contents'
    st = task.state
    if st == 'proposed':
        nxt = 'admit with `wcp task ready %s`' % task_id
    elif st == 'ready':
        nxt = 'claim with `wcp task claim %s --owner <you>`' % task_id
    elif st == 'provisioning':
        if task.lease_expired(at):
            nxt = ('provisioning crashed (lease %s): reclaim with `wcp task claim %s --owner <you>`; '
                   'the scratch worktree is re-provisioned' % (lease, task_id))
        else:
            # release refuses a provisioning task (no lease to force-expire yet): waiting is the only
            # safe action, and an expired lease is what makes the path reclaimable.
            nxt = ('provisioning in progress by %s (lease %s): wait for it to finish; if that process is '
                   'dead, wait for the lease to expire and then reclaim with `wcp task claim %s --owner <you>`'
                   % (task.owner, lease, task_id))
    elif st in ('claimed', 'running', 'blocked', 'conflicted'):
        if task.lease_expired(at):
            nxt = ('lease %s and state is %s: reclaim with `wcp task claim %s --owner <you>`; the worktree and %s '
                   'are retained' % (lease, st, task_id, delta_text))
        elif st == 'conflicted':
            nxt = ('integration refused (%s): owner %s resumes with `wcp task heartbeat %s --owner %s --epoch %s`, '
                   'fixes, and resubmits' % (task.blocked_reason, task.owner, task_id, task.owner, task.epoch))
        else:
            nxt = ('lease %s: owner %s continues; renew with `wcp task heartbeat %s --owner %s --epoch %s`'
                   % (lease, task.owner, task_id, task.owner, task.epoch))
    elif st == 'submitted':
        if sub is not None and sub.epoch != task.epoch:
            nxt = ('submission %s is at a fenced epoch (%d != %d); reclaim with `wcp task claim %s --owner <you>` '
                   'and resubmit' % (sub.id, sub.epoch, task.epoch, task_id))
        else:
            nxt = 'submitted and durable: an integrator runs `wcp task verify %s` (nothing else is needed)' % task_id
    elif st == 'verifying':
        nxt = ('an integrator reserved this task; if no integrator process is alive, run `wcp task verify %s` '
               '(the lock proves it is gone) or `wcp task reject %s --reason ...`' % (task_id, task_id))
    elif st == 'publishing':
        nxt = ('publish was interrupted: run `wcp task verify %s` to complete it idempotently '
               '(third-state paths quarantine as partial_publish)' % task_id)
    else:
        nxt = 'terminal (%s): `wcp task cleanup %s` removes the worktree once every entry is accounted for' % (
            st, task_id)
    return {'task_id': task_id, 'state': st, 'owner': task.owner, 'epoch': task.epoch, 'version': task.version,
            'lease': lease, 'lease_expires_at': task.lease_expires_at, 'worktree': wt, 'worktree_exists': exists,
            'delta_paths': delta, 'delta_error': delta_error, 'submission': submission, 'blockers': blockers,
            'rebase_needed': rebase, 'worktrees': worktrees, 'next_action': nxt}
