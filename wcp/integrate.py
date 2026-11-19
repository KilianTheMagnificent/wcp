"""Integrator: verify/accept/reject under the integrator flock; the only code that writes canonical."""
from __future__ import annotations

import os
from pathlib import Path

from . import bundle, db, gitx, model, paths, scope, tasks
from .db import ProtocolError


def _integration_dir(root: Path) -> Path:
    """Fresh scratch path; a leftover from a crashed integrator is removed (it is ours), unknown state refuses."""
    integ = db.repo_dir(root) / 'integration'
    reg = gitx.registration(root, integ)
    if reg == 'active':
        gitx.remove_worktree(root, integ, force=True)  # integration scratch wcp itself created
        reg = gitx.registration(root, integ)
    if reg != 'absent':
        raise ProtocolError('bad_state', 'stale integration worktree %s (registration %s); run git worktree prune '
                            'in the canonical checkout and inspect the directory' % (integ, reg),
                            {'worktree': str(integ), 'registration': reg})
    return integ


def _accepted_since_claim(conn, task: model.Task) -> bool:
    # ponytail: 'interaction' = any other task accepted since this worktree was provisioned, by wall clock
    row = conn.execute('SELECT created_at FROM worktrees WHERE path=?', (task.worktree_path,)).fetchone()
    since = row['created_at'] if row else task.created_at
    return conn.execute("SELECT 1 FROM tasks WHERE id != ? AND state='accepted' AND updated_at > ?",
                        (task.id, since)).fetchone() is not None


def _fail(conn, task: model.Task, code: str, message: str, detail: dict, dry_run: bool, interaction: bool):
    """Record the verdict (conflicted or back to running) unless dry-run, then raise the typed refusal."""
    state = task.state
    if not dry_run:
        # the event never carries check output (canonical bytes could be read back through `wcp events`);
        # the raised refusal still returns the full receipts to the invocation that ran the checks
        recorded = dict(detail, receipts=[{k: v for k, v in r.items() if k != 'output_tail'}
                                          for r in detail['receipts']]) if 'receipts' in detail else detail
        with db.write_txn(conn):
            cur = model.load_task(conn, task.id)
            if cur.state == 'verifying':
                to = 'conflicted' if (not interaction or _accepted_since_claim(conn, cur)) else 'running'
                cur = model.transition(conn, cur, to, blocked_reason='%s: %s' % (code, message))
                db.add_event(conn, task.id, 'verify_failed', dict(recorded, code=code, to=to))
            state = cur.state
    raise ProtocolError(code, message, dict(detail, task_id=task.id, state=state))


def _run_checks(conn, task_id, sub_id, checks: list[dict], cwd: Path) -> list[dict]:
    """Run the init-declared checks; persist everything but the output.

    A check runs with the integrator's privileges and can read canonical, so its output tail is
    returned only to the invocation that ran it and is stored NULL: `task show`/`recover` must not
    hand canonical bytes to a sandboxed agent that cannot read them itself.
    """
    receipts = []
    for check in checks:
        r = gitx.run_check(check, cwd)
        receipts.append(r)
        with db.write_txn(conn):
            conn.execute('INSERT INTO receipts(task_id, submission_id, phase, cmd, exit_code, duration_ms, output_tail, '
                         'created_at) VALUES (?,?,?,?,?,?,NULL,?)',
                         (task_id, sub_id, 'verify', r['cmd'], r['exit_code'], r['duration_ms'], db.now()))
    return receipts


def verify(conn, root, task_id, holder, dry_run=False, wait_s: float = 0) -> dict:
    """wait_s > 0 (or -1: no limit) polls db.sleep(2) around lock_held only; every other refusal is immediate."""
    deadline = None if wait_s < 0 else db.now() + wait_s
    while True:
        try:
            return _verify(conn, root, task_id, holder, dry_run)
        except ProtocolError as e:
            if e.code != 'lock_held':
                raise
            if deadline is not None and db.now() >= deadline:
                if wait_s == 0:
                    raise
                raise ProtocolError('waiting_timeout', 'integrator lock still held after %ss' % wait_s,
                                    dict(e.detail, task_id=task_id, waited_s=wait_s))
            db.sleep(2)


def _verify(conn, root, task_id, holder, dry_run) -> dict:
    root = Path(root)
    links = tasks.link_dirs(conn)
    with db.integrator_lock(root, holder):  # 1. exactly one integrator (lock_held otherwise)
        with db.write_txn(conn):  # 2. reservation
            task = model.load_task(conn, task_id)
            if task.state not in ('submitted', 'verifying', 'publishing'):
                raise ProtocolError('bad_state', 'task %s is %s; only submitted tasks verify' % (task_id, task.state),
                                    {'task_id': task_id, 'state': task.state})
            sub = tasks.latest_submission(conn, task_id)
            if sub is None:
                raise ProtocolError('bad_state', 'task %s has no submission' % task_id, {'task_id': task_id})
            if task.state != 'publishing' and sub.epoch != task.epoch:
                raise ProtocolError('stale_epoch', 'submission %s is at epoch %d, task at %s' % (
                    sub.id, sub.epoch, task.epoch), {'task_id': task_id, 'submission_id': sub.id,
                                                     'submission_epoch': sub.epoch, 'epoch': task.epoch})
            if task.state == 'submitted' and not dry_run:
                task = model.transition(conn, task, 'verifying')
            db.add_event(conn, task_id, 'verify_started', {'submission': sub.id, 'version': task.version,
                                                           'dry_run': dry_run, 'holder': holder,
                                                           'resume': task.state == 'publishing'})
        if task.state == 'publishing':
            if dry_run:  # report the per-path publish state; recovery writes canonical, dry-run never does
                classes = bundle.classify_publish(root, sub.manifest())
                return {'task_id': task_id, 'submission_id': sub.id, 'state': task.state, 'receipts': [],
                        'warnings': ['publish was interrupted; run verify without --dry-run to complete it'],
                        'published': False, 'paths': [], 'dry_run': True, 'classes': classes}
            return _recover_publish(conn, root, task, sub)
        reserved_version = task.version
        manifest = sub.manifest()
        warnings: list[str] = []
        # 3. gates against canonical
        sc = model.load_scopes(conn, task_id)
        bad_paths = []
        for d in manifest['changes']:
            try:
                rel = paths.normalize_repo_path(d['path'])
            except ProtocolError:
                _fail(conn, task, 'unsafe_path', 'bundle path refused: %r' % d['path'], {'path': d['path']},
                      dry_run, False)
            if not scope.matches(sc['path'], rel):
                bad_paths.append(rel)
        if bad_paths:
            _fail(conn, task, 'out_of_scope', 'bundle paths outside scope %s: %s' % (sc['path'], ', '.join(bad_paths)),
                  {'paths': bad_paths, 'scope': sc['path']}, dry_run, False)
        drift = bundle.check_before_hashes(root, manifest)
        if drift:
            _fail(conn, task, 'before_hash_drift', 'canonical moved under: ' + ', '.join(drift), {'paths': drift},
                  dry_run, False)
        head = gitx.head_revision(root)
        if head != task.base_revision:
            warnings.append('base_revision %s != canonical HEAD %s (per-path hashes are the gate)' % (
                task.base_revision, head))
        # 4-5. integration worktree, apply, fingerprint, checks, fingerprint
        integ = _integration_dir(root)
        gitx.provision_worktree(root, integ, links)
        try:
            drift = bundle.check_before_hashes(integ, manifest)
            if drift:
                _fail(conn, task, 'before_hash_drift', 'integration tree differs from bundle before-state: ' +
                      ', '.join(drift), {'paths': drift}, dry_run, False)
            bundle.apply_bundle(sub.bundle_dir, manifest, integ)
            before = gitx.snapshot(integ, links)
            # Only init-declared checks run here: a task-level --check is declared by whoever ran task create
            # (possibly the builder) and must never execute in the integrator's process; it runs at submit.
            checks = tasks.required_checks(conn)
            receipts = _run_checks(conn, task_id, sub.id, checks, integ)
            after = gitx.snapshot(integ, links)
            mutated = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
            if mutated:
                _fail(conn, task, 'candidate_mutated', 'a check rewrote the candidate: ' + ', '.join(mutated),
                      {'paths': mutated, 'receipts': receipts}, dry_run, True)
            failed = [r for r in receipts if r['exit_code'] != 0]
            if failed:
                _fail(conn, task, 'checks_failed', '%d of %d integrated checks failed: %s' % (
                    len(failed), len(receipts), '; '.join(r['cmd'] for r in failed)),
                      {'receipts': receipts}, dry_run, True)
        finally:
            gitx.remove_worktree(root, integ, force=True)  # 7. our scratch only
        if dry_run:
            return {'task_id': task_id, 'submission_id': sub.id, 'state': task.state, 'receipts': receipts,
                    'warnings': warnings, 'published': False, 'paths': [], 'dry_run': True}
        # 6. publish: CAS + re-hash in one transaction, journal, apply, finish
        drift = []
        with db.write_txn(conn):
            cur = model.load_task(conn, task_id)
            if cur.version != reserved_version or cur.state != 'verifying' or cur.epoch != sub.epoch:
                raise ProtocolError('stale_version', 'task %s changed during verification (state %s, version %d, '
                                    'reserved %d)' % (task_id, cur.state, cur.version, reserved_version),
                                    {'task_id': task_id, 'state': cur.state, 'version': cur.version,
                                     'reserved_version': reserved_version})
            drift = bundle.check_before_hashes(root, manifest)
            if drift:
                cur = model.transition(conn, cur, 'conflicted',
                                       blocked_reason='before_hash_drift: ' + ', '.join(drift))
                db.add_event(conn, task_id, 'verify_failed', {'code': 'before_hash_drift', 'paths': drift,
                                                              'to': 'conflicted'})
            else:
                cur = model.transition(conn, cur, 'publishing')
                db.add_event(conn, task_id, 'publish_started', {'submission': sub.id, 'bundle_dir': sub.bundle_dir,
                                                                'paths': [d['path'] for d in manifest['changes']]})
        if drift:
            raise ProtocolError('before_hash_drift', 'canonical moved during checks: ' + ', '.join(drift),
                                {'task_id': task_id, 'paths': drift, 'state': 'conflicted'})
        touched = bundle.apply_bundle(sub.bundle_dir, manifest, root)
        with db.write_txn(conn):
            cur = model.transition(conn, model.load_task(conn, task_id), 'accepted')
            db.add_event(conn, task_id, 'publish_finished', {'submission': sub.id, 'paths': touched,
                                                             'after': {d['path']: d['after_sha256']
                                                                       for d in manifest['changes']}})
        return {'task_id': task_id, 'submission_id': sub.id, 'state': cur.state, 'receipts': receipts,
                'warnings': warnings, 'published': True, 'paths': touched}


def _recover_publish(conn, root: Path, task: model.Task, sub: model.Submission) -> dict:
    """Idempotent completion of an interrupted publish: after = done, before = apply now, other = quarantine."""
    manifest = sub.manifest()
    classes = bundle.classify_publish(root, manifest)
    other = sorted(p for p, c in classes.items() if c == 'other')
    if other:
        with db.write_txn(conn):
            cur = model.transition(conn, model.load_task(conn, task.id), 'conflicted',
                                   blocked_reason='partial_publish: ' + ', '.join(other))
            db.add_event(conn, task.id, 'partial_publish', {'submission': sub.id, 'paths': other,
                                                            'classes': classes})
        raise ProtocolError('partial_publish', 'canonical paths in neither before nor after state: ' +
                            ', '.join(other) + '; inspect them by hand',
                            {'task_id': task.id, 'submission_id': sub.id, 'paths': other, 'classes': classes,
                             'state': cur.state})
    pending = [p for p, c in classes.items() if c == 'before']
    touched = bundle.apply_bundle(sub.bundle_dir, manifest, root, only=pending) if pending else []
    with db.write_txn(conn):
        cur = model.transition(conn, model.load_task(conn, task.id), 'accepted')
        db.add_event(conn, task.id, 'publish_finished', {'submission': sub.id, 'paths': touched, 'recovered': True,
                                                         'after': {d['path']: d['after_sha256']
                                                                   for d in manifest['changes']}})
    return {'task_id': task.id, 'submission_id': sub.id, 'state': cur.state, 'receipts': [],
            'warnings': ['publish recovered: %d path(s) were already at their after-hash'
                         % sum(1 for c in classes.values() if c == 'after')],
            'published': True, 'paths': touched, 'recovered': True}


def reject(conn, task_id, reason) -> model.Task:
    root = Path(db.meta_get(conn, 'root'))
    with db.integrator_lock(root, 'reject:%d' % os.getpid()):
        with db.write_txn(conn):
            task = model.load_task(conn, task_id)
            if task.state not in ('submitted', 'verifying'):
                raise ProtocolError('bad_state', 'task %s is %s; only submitted/verifying tasks can be rejected'
                                    % (task_id, task.state), {'task_id': task_id, 'state': task.state})
            cur = model.transition(conn, task, 'running', blocked_reason='rejected: ' + reason)
            db.add_event(conn, task_id, 'rejected', {'reason': reason})
            return cur
