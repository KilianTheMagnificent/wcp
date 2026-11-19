"""`wcp run` (addendum Part B): the deterministic lead loop.

Plan -> tasks (one transaction) -> claim as scopes free up -> one builder process per claimed task
(through wcp._attempt) -> submit on the builder's behalf -> verify/publish -> rebase and re-dispatch
on conflict -> pause, never cancel, anything the loop cannot resolve. No process output ever reaches
the database, run.jsonl, or a brief: only paths, codes and exit statuses.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import db, integrate, model, paths, tasks
from .db import ProtocolError

POLL_S = 2.0          # idle wait between iterations (db.sleep)
STATUS_S = 10.0       # status line cadence
KILL_GRACE_S = 10.0   # SIGTERM -> SIGKILL
PLAN_DEFAULTS = {'max_concurrent': 3, 'max_rebase': 2, 'max_attempts': 2, 'auto_approve_scope': False, 'lease_s': 900}
KEEPALIVE_STATES = frozenset({'claimed', 'running', 'blocked', 'conflicted', 'submitted'})
DISPATCHABLE = ('claimed', 'running', 'blocked')
ENV_VARS = ('WCP_HOME', 'WCP_TASK', 'WCP_OWNER', 'WCP_EPOCH', 'WCP_WORKTREE', 'WCP_KEY', 'WCP_ROLE')
BUILDER_ENV_ALLOWLIST = frozenset({
    'HOME', 'PATH', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TERM', 'TMPDIR', 'SHELL',
})
PLACEHOLDERS = ('brief', 'worktree', 'task', 'owner', 'epoch', 'key')
_KEY_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
_PKG_PARENT = str(Path(__file__).resolve().parent.parent)
_WRAPPER = str(Path(__file__).resolve().with_name('_attempt.py'))  # run by path + -I: never the worktree's copy


def _usage(message: str, **detail) -> ProtocolError:
    return ProtocolError('usage', message, detail)


# ---------------------------------------------------------------- plan (B1)

def _str_list(v, what: str) -> list:
    if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
        raise _usage('%s must be a list of strings' % what)
    return list(v)


def validate_plan(raw) -> dict:
    """Closed-key, fail-closed validation; returns the plan with defaults filled. Writes nothing."""
    if not isinstance(raw, dict):
        raise _usage('plan must be a JSON object')
    unknown = sorted(set(raw) - {'version', 'builders', 'tasks'} - set(PLAN_DEFAULTS))
    if unknown:
        raise _usage('unknown plan keys: ' + ', '.join(unknown), keys=unknown)
    if raw.get('version') != 1:
        raise _usage('plan version must be 1')
    plan = dict(PLAN_DEFAULTS)
    plan.update(raw)
    for k in ('max_concurrent', 'max_attempts', 'lease_s'):
        if isinstance(plan[k], bool) or not isinstance(plan[k], int) or plan[k] < 1:
            raise _usage('%s must be an integer >= 1' % k)
    if isinstance(plan['max_rebase'], bool) or not isinstance(plan['max_rebase'], int) or plan['max_rebase'] < 0:
        raise _usage('max_rebase must be an integer >= 0')
    if not isinstance(plan['auto_approve_scope'], bool):
        raise _usage('auto_approve_scope must be true or false')
    builders = plan.get('builders')
    if not isinstance(builders, dict) or not builders:
        raise _usage('builders must be a non-empty object')
    for name, b in builders.items():
        if not isinstance(b, dict) or set(b) - {'argv', 'timeout_s'}:
            raise _usage('builder %s: only argv and timeout_s are allowed' % name)
        argv = _str_list(b.get('argv'), 'builder %s argv' % name)
        if not argv:
            raise _usage('builder %s argv is empty' % name)
        t = b.get('timeout_s', 1800)
        if isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0:
            raise _usage('builder %s timeout_s must be a positive number' % name)
        builders[name] = {'argv': argv, 'timeout_s': t}
    tl = plan.get('tasks')
    if not isinstance(tl, list) or not tl:
        raise _usage('tasks must be a non-empty list')
    keys: set = set()
    out = []
    for t in tl:
        if not isinstance(t, dict):
            raise _usage('each task must be an object')
        extra = sorted(set(t) - {'key', 'goal', 'write', 'resource', 'depends', 'check', 'builder', 'brief'})
        if extra:
            raise _usage('task %s: unknown keys %s' % (t.get('key'), ', '.join(extra)), keys=extra)
        key = t.get('key')
        if not isinstance(key, str) or not _KEY_RE.match(key):
            raise _usage('task key %r must match %s' % (key, _KEY_RE.pattern))
        if key in keys:
            raise _usage('duplicate task key %s' % key, key=key)
        keys.add(key)
        if not isinstance(t.get('goal'), str) or not t['goal'].strip():
            raise _usage('task %s: goal must be a non-empty string' % key)
        if t.get('builder') not in builders:
            raise _usage('task %s: unknown builder %r' % (key, t.get('builder')), key=key)
        if not isinstance(t.get('brief', ''), str):
            raise _usage('task %s: brief must be a string' % key)
        entry = {'key': key, 'goal': t['goal'], 'builder': t['builder'], 'brief': t.get('brief', '')}
        for k in ('write', 'resource', 'depends', 'check'):
            entry[k] = _str_list(t.get(k, []), 'task %s %s' % (key, k))
        try:
            entry['write'] = [paths.normalize_repo_path(p) for p in entry['write']]
        except ProtocolError as e:
            raise _usage('task %s: write pattern refused (%s): %s' % (key, e.code, e.message), key=key, code=e.code)
        bad = [p for p in entry['write'] if paths.is_sensitive(p)]
        if bad:
            raise _usage('task %s: credential-shaped write pattern refused: %s' % (key, ', '.join(bad)), key=key,
                         code='sensitive_path')
        out.append(entry)
    for t in out:
        for d in t['depends']:
            if d not in keys or d == t['key']:
                raise _usage('task %s: unknown dependency %r' % (t['key'], d), key=t['key'])
    plan['tasks'] = out
    _creation_order(out)  # cycle check
    return plan


def _creation_order(plan_tasks: list) -> list:
    by_key = {t['key']: t for t in plan_tasks}
    done: list = []
    seen: set = set()

    def visit(k, stack):
        if k in seen:
            return
        if k in stack:
            raise _usage('dependency cycle through %s' % k, key=k)
        stack.add(k)
        for d in by_key[k]['depends']:
            if d in by_key:  # a dependency outside this batch already exists (resume --plan)
                visit(d, stack)
        stack.discard(k)
        seen.add(k)
        done.append(by_key[k])

    for t in plan_tasks:
        visit(t['key'], set())
    return done


def load_plan(path) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise _usage('cannot read plan %s: %s' % (path, e), path=str(path))
    return validate_plan(raw)


# ---------------------------------------------------------------- builder environment and brief (B1, B7)

def builder_env(base_env: dict, ctx: dict) -> dict:
    """Minimal allowlisted environment plus exactly the six builder variables and WCP_ROLE=builder:
    ambient secrets, loader variables, and any inherited integrator role never reach a builder."""
    env = {k: str(v) for k, v in base_env.items() if k in BUILDER_ENV_ALLOWLIST and v is not None}
    env.update({'WCP_HOME': str(db.DEFAULT_HOME()), 'WCP_TASK': ctx['task_id'], 'WCP_OWNER': ctx['owner'],
                'WCP_EPOCH': str(ctx['epoch']), 'WCP_WORKTREE': ctx['worktree'], 'WCP_KEY': ctx['key'],
                'WCP_ROLE': 'builder'})
    return env


def wcp_command() -> str:
    launcher = Path(_PKG_PARENT) / 'bin' / 'wcp'
    return shlex.quote(str(launcher)) if launcher.is_file() else '%s -m wcp' % shlex.quote(sys.executable)


def brief_for(task: dict, ctx: dict) -> str:
    """Deterministic builder brief (B7). Never contains process output."""
    wcp = ctx.get('wcp') or wcp_command()
    lines = [
        '# wcp builder brief: task %s (%s)' % (ctx['key'], ctx['task_id']),
        '',
        'Role: builder. Task key: %s. Task id: %s. Owner: %s. Epoch: %s.'
        % (ctx['key'], ctx['task_id'], ctx['owner'], ctx['epoch']),
        '',
        '## Goal',
        task['goal'].strip(),
        '',
        '## Where to work',
        '- Your worktree is your current working directory: %s' % ctx['worktree'],
        '- Edit only inside this directory. Never touch the canonical checkout: %s' % ctx['root'],
        '- Do not commit, push, or run any git mutation. Do not run `wcp task verify`, `wcp task accept`, '
        'or `wcp task submit`: the runner submits, verifies, and keeps your lease alive.',
        '- When finished, leave the worktree in its finished state and exit 0. Exit non-zero to report failure.',
        '',
        '## Allowed write scope',
    ]
    lines += ['- path: %s' % p for p in sorted(task.get('write', []))] or ['- (no path scope)']
    lines += ['- resource: %s' % r for r in sorted(task.get('resource', []))]
    checks = task.get('check', [])
    if checks:
        lines += ['', '## Run these before finishing (the integrator runs the repository checks again after you)']
        lines += ['- %s' % c for c in checks]
    lines += [
        '',
        '## Requesting more scope',
        '- If you can run commands: `%s scope request %s --owner %s --epoch %s --write <pattern> --reason <why>`, '
        'then poll `%s task show %s --json` (up to 5 minutes) until the request is approved before writing there.'
        % (wcp, ctx['task_id'], ctx['owner'], ctx['epoch'], wcp, ctx['task_id']),
        '- If you cannot: print one line `SCOPE-NEEDED: <path>` and exit 0 without editing anything; '
        'you will be re-dispatched once the scope is approved.',
    ]
    obligations = ctx.get('obligations') or []
    if obligations:
        lines += ['', '## Rebase obligations',
                  'Canonical moved under these paths. Your earlier versions are in the retired worktree %s, '
                  'not in this one:' % (ctx.get('retired_worktree') or obligations[0].get('retired_worktree'))]
        lines += ['- %s' % o['path'] for o in obligations]
        lines += ['For each path either re-apply your change against the new baseline in this worktree, or print '
                  'one line `KEEP-CANONICAL: <path>` (canonical\'s version stands). Submission is refused while '
                  'any of them is unresolved.']
    prev = []
    if ctx.get('note'):
        prev.append('- %s' % ctx['note'])
    if ctx.get('previous_log'):
        prev.append('- The previous attempt exited %s; its log is at %s (not reproduced here).'
                    % (ctx.get('exit_code'), ctx['previous_log']))
    fc = ctx.get('failed_check')
    if fc:
        prev.append('- The repository check `%s` exited %s on the integrated candidate. Run it in this worktree '
                    'to see the failure.' % (fc['cmd'], fc['exit_code']))
    if prev:
        lines += ['', '## Previous attempt'] + prev
    if task.get('brief'):
        lines += ['', '## Additional instructions', task['brief']]
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------- keep-alive (B4)

class _KeepAlive(threading.Thread):
    """Own connection; heartbeats every runner-owned, unfinished task whose WCP state still holds a lease,
    regardless of builder liveness. Stops only when the row is accepted/paused or the task is
    integrator-owned or done."""

    def __init__(self, root: Path, run_id: str, lease_s: int):
        super().__init__(name='wcp-keepalive', daemon=True)
        self.root, self.run_id, self.lease_s = root, run_id, lease_s
        self.stop = threading.Event()

    def run(self):
        conn = db.open_db(self.root)
        try:
            while True:  # first tick immediately: an adopted lease may have less than lease_s/3 left
                try:
                    self.tick(conn)
                except Exception:
                    pass  # ponytail: a failed tick is retried next interval; nothing else to do from here
                if self.stop.wait(self.lease_s / 3.0):
                    break
        finally:
            conn.close()

    def tick(self, conn):
        rows = conn.execute("SELECT key, task_id, status FROM run_tasks WHERE run_id=? "
                            "AND status NOT IN ('accepted','paused')", (self.run_id,)).fetchall()
        for r in rows:
            task = model.load_task(conn, r['task_id'])
            owner = 'run:%s:%s' % (self.run_id, r['key'])
            if task.state not in KEEPALIVE_STATES or task.owner != owner or task.lease_expired(db.now()):
                continue  # a lapsed lease is the loop's decision (re-arm or runner_reclaim, B6), never this thread's
            try:  # lease only: the task's state (conflicted, claimed, ...) is the loop's to route
                tasks.runner_heartbeat(conn, task.id, owner, self.lease_s)
            except ProtocolError:
                pass  # owner or state changed under us: the next tick re-reads


# ---------------------------------------------------------------- runner

def _crash_point(name: str) -> None:
    # ponytail: fault injection for the runner-death tests; a real run never sets WCP_RUN_CRASH_AT
    if os.environ.get('WCP_RUN_CRASH_AT') == name:
        os._exit(70)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _reason_code(blocked_reason) -> str:
    return (blocked_reason or '').split(':', 1)[0]


def _reason_paths(blocked_reason) -> list:
    _, _, rest = (blocked_reason or '').partition(':')
    return [p.strip() for p in rest.split(',') if p.strip()]


class Runner:
    def __init__(self, conn, root, plan=None, run_id=None, quiet=False, max_iterations=None):
        self.conn = conn
        self.root = Path(root)
        self.plan = plan
        self.run_id = run_id
        self.quiet = quiet
        self.max_iterations = max_iterations
        self.run_dir: Path = None  # type: ignore[assignment]
        self._lock_fd = None
        self._procs: dict = {}
        self._admitted = 0  # live builders as of the current iteration's start, plus what it dispatched
        self._reported_requests: set = set()
        self._last_lines = None
        self._last_print = 0.0

    # -- small helpers -----------------------------------------------------------------------
    def owner(self, key: str) -> str:
        return 'run:%s:%s' % (self.run_id, key)

    def _plan_task(self, key: str) -> dict:
        for t in self.plan['tasks']:
            if t['key'] == key:
                return t
        raise ProtocolError('not_found', 'task key %s is not in the plan' % key, {'key': key})

    def _accepted_task(self, task_id: str) -> bool:
        row = self.conn.execute('SELECT state FROM tasks WHERE id=?', (task_id,)).fetchone()
        return row is not None and row['state'] == 'accepted'

    def _rts(self) -> list:
        return self.conn.execute('SELECT * FROM run_tasks WHERE run_id=? ORDER BY rowid', (self.run_id,)).fetchall()

    def _rt(self, key: str):
        row = self.conn.execute('SELECT * FROM run_tasks WHERE run_id=? AND key=?', (self.run_id, key)).fetchone()
        if row is None:
            raise ProtocolError('not_found', 'run %s has no task %s' % (self.run_id, key), {'key': key})
        return row

    def _set_rt(self, key: str, **cols) -> None:
        with db.write_txn(self.conn):
            self.conn.execute('UPDATE run_tasks SET %s WHERE run_id=? AND key=?' % ', '.join(c + '=?' for c in cols),
                              list(cols.values()) + [self.run_id, key])

    def _set_run(self, status: str) -> None:
        with db.write_txn(self.conn):
            self.conn.execute('UPDATE runs SET status=?, updated_at=? WHERE id=?', (status, db.now(), self.run_id))

    def _latest_attempt(self, key: str):
        return self.conn.execute('SELECT * FROM run_attempts WHERE run_id=? AND key=? ORDER BY attempt DESC LIMIT 1',
                                 (self.run_id, key)).fetchone()

    def _attempt_path(self, key: str, n: int, suffix: str) -> Path:
        return self.run_dir / ('attempt-%s-%d.%s' % (key, n, suffix))

    def _event(self, kind: str, **fields) -> None:
        """Append one line to run.jsonl: paths, codes, and ids only; never process output."""
        line = json.dumps(dict(fields, at=db.now(), event=kind), sort_keys=True, default=str)
        fd = os.open(str(self.run_dir / 'run.jsonl'), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as f:
            f.write(line + '\n')

    def _write_private(self, path: Path, text: str) -> None:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)

    # -- runner lock (B6) --------------------------------------------------------------------
    def _acquire_lock(self) -> None:
        if self._lock_fd is not None:
            return
        path = self.run_dir / 'runner.lock'
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise ProtocolError('lock_held', 'another runner holds %s' % path, {'lock': str(path), 'run_id': self.run_id})
        self._lock_fd = fd

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    # -- start / resume ----------------------------------------------------------------------
    def start(self) -> str:
        if self.plan is None:
            raise _usage('a plan is required to start a run')
        if not tasks.required_checks(self.conn):
            raise _usage('wcp run refuses a repository with no init-declared required checks; '
                         'run `wcp init --check CMD` first')
        plan_json = json.dumps(self.plan, sort_keys=True)
        sha = hashlib.sha256(plan_json.encode('utf-8')).hexdigest()
        with db.write_txn(self.conn):
            while True:
                run_id = db.new_id('r-')
                if self.conn.execute('SELECT 1 FROM runs WHERE id=?', (run_id,)).fetchone() is None:
                    break
            at = db.now()
            self.conn.execute('INSERT INTO runs(id, plan_sha256, plan_json, root, created_at, updated_at, status) '
                              "VALUES (?,?,?,?,?,?,'running')", (run_id, sha, plan_json, str(self.root), at, at))
            self._create_tasks(run_id, self.plan['tasks'], {})
        self.run_id = run_id
        self.run_dir = db.run_dir(self.root, run_id)
        self._event('created', run_id=run_id, plan_sha256=sha,
                    tasks={r['key']: r['task_id'] for r in self._rts()})
        return run_id

    def _create_tasks(self, run_id: str, plan_tasks: list, ids: dict) -> None:
        """Inside the caller's write_txn: every task, scope, dep and run_tasks row, or nothing."""
        for t in _creation_order(plan_tasks):
            if t['key'] in ids:
                continue
            task = tasks.create_task_in_txn(self.conn, t['goal'], t['write'], t['resource'],
                                            [ids[d] for d in t['depends']])
            ids[t['key']] = task.id
        for t in plan_tasks:
            self.conn.execute('INSERT INTO run_tasks(run_id, key, task_id, builder, attempts, rebases, status, last_error) '
                              "VALUES (?,?,?,?,0,0,'pending',NULL)", (run_id, t['key'], ids[t['key']], t['builder']))

    def resume(self, retry_keys=(), retry_all=False) -> None:
        row = self.conn.execute('SELECT * FROM runs WHERE id=?', (self.run_id,)).fetchone()
        if row is None:
            raise ProtocolError('not_found', 'no run %s' % self.run_id, {'run_id': self.run_id})
        self.run_dir = db.run_dir(self.root, self.run_id)
        self._acquire_lock()
        try:
            stored = json.loads(row['plan_json'])
            if self.plan is None:
                self.plan = stored
            else:
                self._merge_plan(stored)
            rts = {r['key']: r for r in self._rts()}
            keys = [k for k, r in rts.items() if r['status'] == 'paused'] if retry_all else list(retry_keys)
            for k in keys:
                r = rts.get(k)
                if r is None:
                    raise _usage('--retry %s: no such task in run %s' % (k, self.run_id), key=k)
                if r['status'] != 'paused':
                    raise _usage('--retry %s: task is %s, not paused' % (k, r['status']), key=k)
                quarantined = _reason_code(model.load_task(self.conn, r['task_id']).blocked_reason) == 'partial_publish'
                if r['last_error'] == 'partial_publish' or quarantined:
                    raise _usage('--retry %s: partial_publish is never retried automatically; inspect the listed '
                                 'canonical paths and run `wcp task verify %s` by hand' % (k, r['task_id']), key=k)
            with db.write_txn(self.conn):
                for k in keys:
                    self.conn.execute("UPDATE run_tasks SET status='pending', attempts=0, rebases=0, last_error=NULL "
                                      'WHERE run_id=? AND key=?', (self.run_id, k))
                self.conn.execute("UPDATE runs SET status='running', updated_at=? WHERE id=?", (db.now(), self.run_id))
            self._event('resumed', run_id=self.run_id, retry=keys)
        except BaseException:
            self._release_lock()
            raise

    def _merge_plan(self, stored: dict) -> None:
        """--plan on resume: existing keys must match byte-for-byte; new keys are created in one transaction."""
        old = {t['key']: t for t in stored['tasks']}
        new = {t['key']: t for t in self.plan['tasks']}
        for k, t in old.items():
            if k not in new:
                raise _usage('--plan drops existing task %s' % k, key=k)
            for f in ('write', 'resource', 'depends'):
                if json.dumps(t[f]) != json.dumps(new[k][f]):
                    raise _usage('--plan changes %s of existing task %s' % (f, k), key=k)
        added = [t for t in self.plan['tasks'] if t['key'] not in old]
        plan_json = json.dumps(self.plan, sort_keys=True)
        sha = hashlib.sha256(plan_json.encode('utf-8')).hexdigest()
        with db.write_txn(self.conn):
            ids = {r['key']: r['task_id'] for r in self._rts()}
            self._create_tasks(self.run_id, added, ids)
            self.conn.execute('UPDATE runs SET plan_json=?, plan_sha256=?, updated_at=? WHERE id=?',
                              (plan_json, sha, db.now(), self.run_id))
        if added:
            self._event('created', run_id=self.run_id, plan_sha256=sha,
                        tasks={t['key']: ids[t['key']] for t in added})

    # -- loop (B5) ---------------------------------------------------------------------------
    def loop(self) -> dict:
        self._acquire_lock()
        keepalive = _KeepAlive(self.root, self.run_id, int(self.plan['lease_s']))
        keepalive.start()
        summary = None
        try:
            it = 0
            while True:
                it += 1
                self._iterate()
                summary = self.summary()
                self._print_status(summary)
                if summary['status'] in ('done', 'paused'):
                    break
                if self.max_iterations is not None and it >= self.max_iterations:
                    break
                db.sleep(POLL_S)
        except KeyboardInterrupt:
            for rt in self._rts():
                att = self._latest_attempt(rt['key'])
                if att is not None and self._attempt_live(rt['key'], att['attempt']):
                    self._signal(self._pgid(att), signal.SIGTERM)
            summary = dict(self.summary(), status='paused', interrupted=True)
            self._event('paused', run_id=self.run_id, reason='interrupted')
        finally:
            keepalive.stop.set()
            keepalive.join(timeout=30)
            self._release_lock()
        self._set_run(summary['status'])
        if summary['status'] == 'done':
            self._event('done', run_id=self.run_id, accepted=summary['accepted'])
        elif summary['status'] == 'paused' and not summary.get('interrupted'):
            self._event('paused', run_id=self.run_id, paused=summary['paused'])
        return summary

    def summary(self) -> dict:
        rts = self._rts()
        accepted = [r['key'] for r in rts if r['status'] == 'accepted']
        paused = [{'key': r['key'], 'last_error': r['last_error']} for r in rts if r['status'] == 'paused']
        in_flight = any(r['status'] in ('building', 'submit_pending', 'verifying') for r in rts)
        if len(accepted) == len(rts):
            status = 'done'
        elif paused and not in_flight and self._live_count() == 0:
            status = 'paused'
        else:
            status = 'running'
        return {'run_id': self.run_id, 'status': status, 'accepted': accepted, 'paused': paused}

    def _iterate(self) -> None:
        # One admission snapshot per iteration: a builder seen alive when the iteration began stays counted
        # until it has been reaped, so a slot is never handed out on an observation fresher than the reap.
        self._admitted = self._live_count()
        for rt in self._rts():
            if rt['status'] in ('accepted', 'paused'):
                continue
            try:
                self._step(rt)
            except KeyboardInterrupt:
                raise
            except ProtocolError as e:  # catch-all (B5.4): durable pause, nothing cancelled or cleaned
                self._pause(rt['key'], e.code, paths=e.detail.get('paths'))
            except Exception as e:
                self._pause(rt['key'], type(e).__name__, message=str(e)[:500])

    def _step(self, rt) -> None:
        key = rt['key']
        task = model.load_task(self.conn, rt['task_id'])
        if task.state == 'accepted':
            return self._accepted(key, task)
        att = self._latest_attempt(key)
        live = att is not None and self._attempt_live(key, att['attempt'])
        if task.state == 'cancelled':  # only a lead cancels: its builder stops, the row reports, nothing else
            if live:
                self._kill(key, att['attempt'], self._pgid(att))
                self._event('builder_killed', key=key, task=task.id, attempt=att['attempt'], reason='cancelled')
            return self._pause(key, 'cancelled')
        if rt['status'] != 'pending':
            task = self._rearm(key, task)
        if live:  # never re-dispatch, rebase, reclaim, or submit while an attempt is live (B6), retried rows included
            self._approve_live_requests(key, task)
            return self._check_timeout(rt, att)
        if _reason_code(task.blocked_reason) == 'partial_publish':  # quarantine: never rebased, reclaimed, or resubmitted
            return self._pause(key, 'partial_publish', paths=_reason_paths(task.blocked_reason))
        if task.state == 'provisioning' and task.owner == self.owner(key):
            return self._reclaim(rt)  # the previous runner died mid-claim or mid-rebase, whatever the row says (B6)
        if rt['status'] == 'pending':
            return self._step_pending(rt, task, att)
        if rt['status'] == 'building':
            return self._step_building(rt, task, att)
        if rt['status'] == 'submit_pending':
            return self._step_after_exit(rt, task, keeps_acked=True)
        if rt['status'] == 'verifying':
            return self._step_verifying(rt, task)

    def _rearm(self, key: str, task):
        """A runner-owned lease that lapsed (the previous runner died, or a verify outlasted it) is renewed before
        anything owner-gated runs; runner.lock proves the owner string is ours, and the transaction inside refuses
        if a stranger reclaimed meanwhile. Retried (pending) rows are exempt: B6 reclaims those instead."""
        if task.state in KEEPALIVE_STATES and task.owner == self.owner(key) and task.lease_expired(db.now()):
            task = tasks.runner_heartbeat(self.conn, task.id, self.owner(key), int(self.plan['lease_s']))
            self._event('lease_rearmed', key=key, task=task.id, epoch=task.epoch)
        return task

    def _step_pending(self, rt, task, att=None) -> None:
        key = rt['key']
        if task.state == 'proposed':
            task = tasks.ready_task(self.conn, task.id)
        if task.state == 'ready':
            if self._admitted >= int(self.plan['max_concurrent']):
                return
            if not all(self._accepted_task(d) for d in model.load_deps(self.conn, task.id)):
                return
            try:
                out = tasks.claim_task(self.conn, self.root, task.id, self.owner(key), int(self.plan['lease_s']))
            except ProtocolError as e:
                if e.code in ('scope_overlap', 'deps_unmet'):
                    return  # not yet: the loop itself is the wait
                raise
            _crash_point('claim')
            # a claim after a failed re-provision transfers from the worktree that re-provision retired (A4)
            return self._dispatch(rt, obligations=out.get('rebase_needed', []), retired=out.get('retired_worktree'))
        if task.owner != self.owner(key):
            raise ProtocolError('not_owner', 'task %s is owned by %s' % (task.id, task.owner),
                                {'task_id': task.id, 'owner': task.owner})
        expired = task.lease_expired(db.now())
        if task.state == 'provisioning' or (task.state in KEEPALIVE_STATES and expired):
            return self._reclaim(rt)
        if att is not None and att['exited_at'] is None:
            return self._step_building(rt, task, att)  # paused while its attempt was live: reap that attempt first
        if task.state == 'conflicted':
            return self._rebase(rt, task)
        if task.state in DISPATCHABLE:
            return self._dispatch(rt, note='re-dispatched by --retry', reason='retry')
        if task.state in ('submitted', 'verifying', 'publishing'):
            return self._verify(rt, task)
        raise ProtocolError('bad_state', 'task %s is %s' % (task.id, task.state), {'task_id': task.id, 'state': task.state})

    def _step_building(self, rt, task, att) -> None:
        key = rt['key']
        if att is None:
            return self._step_pending(rt, task)
        n = att['attempt']
        exitj = _read_json(self._attempt_path(key, n, 'exit.json'))
        if att['exited_at'] is None and self._never_started(key, n):
            with db.write_txn(self.conn):
                self.conn.execute('UPDATE run_attempts SET exited_at=? WHERE run_id=? AND key=? AND attempt=?',
                                  (db.now(), self.run_id, key, n))
            self._event('builder_exited', key=key, attempt=n, task=task.id, exit_code=None, never_started=True)
            if n > 1 and self._never_started(key, n - 1):  # bounded: two in a row is the wrapper failing, not a crash
                return self._pause(key, 'builder_lost', attempt=n)
            return self._dispatch(rt, count=False, note='the previous attempt was never started', reason='never_started')
        code = exitj.get('exit_code') if exitj else None
        already = att['exited_at'] is not None
        if not already:
            with db.write_txn(self.conn):
                self.conn.execute('UPDATE run_attempts SET exited_at=?, exit_code=? WHERE run_id=? AND key=? AND attempt=?',
                                  ((exitj or {}).get('exited_at') or db.now(), code, self.run_id, key, n))
            self._event('builder_exited', key=key, attempt=n, task=task.id, exit_code=code, log=att['log_path'])
            _crash_point('reaped')
        if already and task.state in DISPATCHABLE:
            rc = _reason_code(task.blocked_reason)
            if rc in ('before_hash_drift', 'candidate_mutated'):
                return self._rebase(rt, task)  # the previous runner died after a verify verdict
        self._step_after_exit(rt, task, att=att, code=code)

    def _step_after_exit(self, rt, task, att=None, code=0, keeps_acked=False) -> None:
        """Reap precedence (B5.2): markers, then exit status, then submit; routing by resulting state."""
        key = rt['key']
        if task.state == 'conflicted':
            return self._rebase(rt, task)
        if task.state in ('submitted', 'verifying', 'publishing'):
            return self._verify(rt, task)
        if task.state not in DISPATCHABLE:
            raise ProtocolError('bad_state', 'task %s is %s after its builder exited' % (task.id, task.state),
                                {'task_id': task.id, 'state': task.state})
        keeps: list = []
        if att is not None:
            scopes, keeps, rejected = self._markers(key, att['attempt'])
            if keeps and not keeps_acked:
                acked, unknown = [], []
                for p in keeps:
                    try:
                        tasks.ack_obligation(self.conn, task.id, self.owner(key), task.epoch, p)
                        acked.append(p)
                    except ProtocolError as e:
                        if e.code != 'not_found':
                            raise
                        unknown.append(p)
                self._event('keep_canonical', key=key, task=task.id, acked=acked, unknown=unknown, rejected=rejected)
            if scopes:
                if not self._approve_paths(key, task, scopes, 'SCOPE-NEEDED marker'):
                    return
                if code == 0:
                    return self._dispatch(rt, count=False, note='scope approved for: ' + ', '.join(scopes),
                                          reason='scope_approved')
            if code != 0:
                return self._failed(rt, task, 'builder_failed' if code is not None else 'builder_lost',
                                    previous_log=att['log_path'], exit_code=code)
            if keeps and not keeps_acked:
                self._set_rt(key, status='submit_pending')  # submitted on the next iteration, never in this reap
                return
        self._submit(rt, task)

    def _step_verifying(self, rt, task) -> None:
        if task.state in ('submitted', 'verifying', 'publishing'):
            return self._verify(rt, task)
        if task.state == 'conflicted':
            return self._rebase(rt, task)
        if task.state in DISPATCHABLE:  # the verdict landed but the previous runner died before routing it
            rc = _reason_code(task.blocked_reason)
            if rc in ('before_hash_drift', 'candidate_mutated'):
                return self._rebase(rt, task)
            if rc == 'checks_failed':
                return self._failed(rt, task, 'checks_failed', failed_check=self._failed_check(task.id))
            return self._submit(rt, task)
        raise ProtocolError('bad_state', 'task %s is %s while verifying' % (task.id, task.state),
                            {'task_id': task.id, 'state': task.state})

    # -- actions -----------------------------------------------------------------------------
    def _accepted(self, key: str, task) -> None:
        self._set_rt(key, status='accepted', last_error=None)
        self._event('published', key=key, task=task.id)

    def _pause(self, key: str, code: str, **fields) -> None:
        self._set_rt(key, status='paused', last_error=code)
        self._event('paused', key=key, code=code, **{k: v for k, v in fields.items() if v is not None})

    def _submit(self, rt, task) -> None:
        key = rt['key']
        for _ in range(3):  # out_of_scope -> approve -> submit again; the same paths cannot refuse twice
            task = model.load_task(self.conn, task.id)
            try:
                out = tasks.submit(self.conn, self.root, task.id, self.owner(key), task.epoch)
            except ProtocolError as e:
                if e.code == 'out_of_scope':
                    if self._approve_paths(key, task, e.detail.get('paths') or [], 'out_of_scope at submit'):
                        continue
                    return
                if e.code == 'rebase_pending':
                    obligations = [{'path': p, 'retired_worktree': e.detail.get('retired_worktree')}
                                   for p in e.detail.get('paths') or []]
                    return self._failed(rt, task, 'rebase_pending', obligations=obligations,
                                        retired=e.detail.get('retired_worktree'))
                if e.code == 'bad_state' and 'nothing changed' in e.message:
                    return self._failed(rt, task, 'nothing_changed')
                raise
            self._event('submitted', key=key, task=task.id, submission=out.get('submission_id'),
                        paths=[c['path'] for c in out.get('changes', [])])
            _crash_point('submit')
            return self._verify(rt, task)
        raise ProtocolError('out_of_scope', 'scope approval did not converge for %s' % task.id, {'task_id': task.id})

    def _verify(self, rt, task) -> None:
        key = rt['key']
        self._set_rt(key, status='verifying')
        self._event('verify', key=key, task=task.id)
        _crash_point('verify')
        try:
            out = integrate.verify(self.conn, self.root, task.id, self.owner(key), wait_s=-1)
        except ProtocolError as e:
            state = e.detail.get('state')
            if e.code == 'partial_publish':
                return self._pause(key, 'partial_publish', paths=e.detail.get('paths'))
            task = self._rearm(key, model.load_task(self.conn, task.id))  # no heartbeat while verifying (B4)
            if state == 'conflicted' or (state == 'running' and e.code in ('before_hash_drift', 'candidate_mutated')):
                return self._rebase(rt, task, failed_check=self._failed_check(task.id) if e.code == 'checks_failed' else None)
            if state == 'running' and e.code == 'checks_failed':
                return self._failed(rt, task, 'checks_failed', failed_check=self._failed_check(task.id))
            raise
        self._event('published', key=key, task=task.id, submission=out.get('submission_id'), paths=out.get('paths'))
        self._set_rt(key, status='accepted', last_error=None)

    def _failed_check(self, task_id: str):
        row = self.conn.execute("SELECT cmd, exit_code FROM receipts WHERE task_id=? AND phase='verify' AND exit_code != 0 "
                                'ORDER BY id DESC LIMIT 1', (task_id,)).fetchone()
        return None if row is None else {'cmd': row['cmd'], 'exit_code': row['exit_code']}

    def _rebase(self, rt, task, failed_check=None) -> None:
        key = rt['key']
        if rt['rebases'] >= int(self.plan['max_rebase']):
            return self._pause(key, 'conflict_exhausted')
        task = model.load_task(self.conn, task.id)
        out = tasks.rebase_task(self.conn, self.root, task.id, self.owner(key), task.epoch, int(self.plan['lease_s']))
        with db.write_txn(self.conn):
            self.conn.execute('UPDATE run_tasks SET rebases=rebases+1 WHERE run_id=? AND key=?', (self.run_id, key))
        self._event('rebased', key=key, task=task.id, epoch=out['epoch'], worktree=out['worktree'],
                    retired_worktree=out.get('retired_worktree'),
                    obligations=[o['path'] for o in out.get('rebase_needed', [])])
        self._dispatch(self._rt(key), obligations=out.get('rebase_needed', []), retired=out.get('retired_worktree'),
                       failed_check=failed_check, note='re-dispatched after a rebase', reason='rebase')

    def _reclaim(self, rt) -> None:
        key = rt['key']
        out = tasks.runner_reclaim(self.conn, self.root, rt['task_id'], self.owner(key), int(self.plan['lease_s']))
        self._event('reclaimed', key=key, task=rt['task_id'], epoch=out['epoch'], worktree=out['worktree'],
                    retired_worktree=out.get('retired_worktree'), recovered=[e['path'] for e in out.get('recovered', [])],
                    obligations=[o['path'] for o in out.get('rebase_needed', [])])
        self._dispatch(rt, obligations=out.get('rebase_needed', []), retired=out.get('retired_worktree'),
                       note='re-dispatched after the previous runner died', reason='runner_reclaim')

    def _failed(self, rt, task, reason: str, previous_log=None, exit_code=None, obligations=(), retired=None,
                failed_check=None) -> None:
        key = rt['key']
        if rt['attempts'] < int(self.plan['max_attempts']):
            return self._dispatch(rt, note='previous attempt failed: %s' % reason, previous_log=previous_log,
                                  exit_code=exit_code, obligations=obligations, retired=retired,
                                  failed_check=failed_check, reason=reason)
        self._pause(key, 'builder_failed', reason=reason, exit_code=exit_code)

    def _approve_paths(self, key: str, task, paths: list, source: str) -> bool:
        """Scope for exactly these paths through the arbiter; False = paused scope_pending."""
        auto = bool(self.plan['auto_approve_scope'])
        for p in paths:
            task = model.load_task(self.conn, task.id)
            req = tasks.request_scope(self.conn, task.id, self.owner(key), task.epoch, 'path', p, 'wcp run: ' + source)
            if not auto:
                self._event('scope_pending', key=key, task=task.id, path=p, request=req, reason='auto_approve_scope off')
                self._pause(key, 'scope_pending', path=p, request=req)
                return False
            try:
                tasks.approve_scope(self.conn, task.id, req)
            except ProtocolError as e:
                if e.code != 'scope_overlap':
                    raise
                self._event('scope_pending', key=key, task=task.id, path=p, request=req, other=e.detail.get('other'))
                self._pause(key, 'scope_pending', path=p, request=req)
                return False
            self._event('scope_approved', key=key, task=task.id, path=p, request=req)
        return True

    def _approve_live_requests(self, key: str, task) -> None:
        """B5.3: requests a live builder raised through the CLI."""
        for r in self.conn.execute("SELECT id, kind, pattern FROM scope_requests WHERE task_id=? AND status='pending' "
                                   'ORDER BY id', (task.id,)).fetchall():
            if r['id'] in self._reported_requests:
                continue
            if not bool(self.plan['auto_approve_scope']):
                self._reported_requests.add(r['id'])
                self._event('scope_pending', key=key, task=task.id, path=r['pattern'], request=r['id'],
                            reason='auto_approve_scope off')
                continue
            try:
                tasks.approve_scope(self.conn, task.id, r['id'])
                self._event('scope_approved', key=key, task=task.id, path=r['pattern'], request=r['id'])
            except ProtocolError as e:
                self._reported_requests.add(r['id'])
                self._event('scope_pending', key=key, task=task.id, path=r['pattern'], request=r['id'], code=e.code)

    # -- attempts (B3) -----------------------------------------------------------------------
    def _dispatch(self, rt, count=True, note=None, previous_log=None, exit_code=None, obligations=(), retired=None,
                  failed_check=None, reason=None) -> None:
        key = rt['key']
        task = model.load_task(self.conn, rt['task_id'])
        if task.state not in DISPATCHABLE or task.owner != self.owner(key):
            raise ProtocolError('bad_state', 'task %s is %s (owner %s); cannot dispatch' % (task.id, task.state, task.owner),
                                {'task_id': task.id, 'state': task.state})
        prev = self._latest_attempt(key)
        n = (prev['attempt'] if prev is not None else 0) + 1
        pt = self._plan_task(key)
        builder = self.plan['builders'][pt['builder']]
        ctx = {'key': key, 'task_id': task.id, 'owner': self.owner(key), 'epoch': task.epoch,
               'worktree': task.worktree_path, 'root': str(self.root), 'wcp': wcp_command(), 'attempt': n,
               'obligations': list(obligations), 'retired_worktree': retired, 'failed_check': failed_check,
               'previous_log': previous_log, 'exit_code': exit_code, 'note': note}
        brief = brief_for(pt, ctx)
        log_path = str(self.run_dir / ('builder-%s-%d.log' % (key, n)))
        with db.write_txn(self.conn):  # intent before spawn
            self.conn.execute("UPDATE run_tasks SET status='building', attempts=attempts+? WHERE run_id=? AND key=?",
                              (1 if count else 0, self.run_id, key))
            self.conn.execute('INSERT INTO run_attempts(run_id, key, attempt, task_epoch, log_path, pgid, started_at, '
                              'exited_at, exit_code) VALUES (?,?,?,?,?,NULL,?,NULL,NULL)',
                              (self.run_id, key, n, task.epoch, log_path, db.now()))
        _crash_point('intent')
        self._write_private(self.run_dir / ('brief-%s-%d.md' % (key, n)), brief)
        subst = {'brief': brief, 'worktree': task.worktree_path, 'task': task.id, 'owner': self.owner(key),
                 'epoch': str(task.epoch), 'key': key}
        argv = []
        for a in builder['argv']:
            for name in PLACEHOLDERS:
                a = a.replace('{%s}' % name, subst[name])
            argv.append(a)
        pgid = self._spawn(key, n, argv, ctx)
        self._admitted += 1
        _crash_point('spawn')
        with db.write_txn(self.conn):
            self.conn.execute('UPDATE run_attempts SET pgid=? WHERE run_id=? AND key=? AND attempt=?',
                              (pgid, self.run_id, key, n))
        self._event('dispatched' if n == 1 else 'redispatched', key=key, task=task.id, attempt=n, epoch=task.epoch,
                    worktree=task.worktree_path, pgid=pgid, log=log_path, builder=pt['builder'], reason=reason)

    def _spawn(self, key: str, n: int, argv: list, ctx: dict) -> int:
        lock_path = self._attempt_path(key, n, 'lock')
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ProtocolError('bad_state', 'attempt lock %s is already held' % lock_path, {'lock': str(lock_path)})
            env = builder_env(os.environ, ctx)
            # by absolute path, isolated: neither the worktree (cwd) nor PYTHONPATH can substitute the wrapper
            cmd = [sys.executable, '-I', _WRAPPER, str(self.run_dir), key, str(n), str(fd), '--'] + argv
            p = subprocess.Popen(cmd, cwd=ctx['worktree'], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=(fd,))
        finally:
            os.close(fd)  # the wrapper's inherited descriptor keeps the lock; ours is not needed
        self._procs[(key, n)] = p
        return p.pid  # session leader: pid == pgid

    def _attempt_live(self, key: str, n: int) -> bool:
        p = self._procs.get((key, n))
        if p is not None:
            p.poll()  # collect our own exited wrapper; liveness is still decided by the lock
        fd = os.open(str(self._attempt_path(key, n, 'lock')), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            return False
        finally:
            os.close(fd)

    def _live_count(self) -> int:
        """Every live attempt counts, a paused row's included: the run neither exits nor over-admits while a
        builder is alive."""
        n = 0
        for r in self._rts():
            att = self._latest_attempt(r['key'])
            if att is not None and self._attempt_live(r['key'], att['attempt']):
                n += 1
        return n

    def _never_started(self, key: str, n: int) -> bool:
        return not (self._attempt_path(key, n, 'started.json').exists() or self._attempt_path(key, n, 'exit.json').exists())

    def _markers(self, key: str, n: int):
        """(scope paths, keep paths, rejected count) from the wrapper's markers file; only values that normalize
        as repository paths are ever acted on or written to run.jsonl."""
        scopes, keeps, rejected = [], [], 0
        for m in _read_json(self._attempt_path(key, n, 'markers.json')) or []:
            try:
                rel = paths.normalize_repo_path(m.get('value') or '')
            except ProtocolError:
                rejected += 1
                continue
            if m.get('marker') == 'SCOPE-NEEDED':
                scopes.append(rel)
            elif m.get('marker') == 'KEEP-CANONICAL':
                keeps.append(rel)
        return scopes, keeps, rejected

    def _pgid(self, att):
        if att['pgid']:
            return att['pgid']
        started = _read_json(self._attempt_path(att['key'], att['attempt'], 'started.json')) or {}
        return started.get('pgid')

    def _signal(self, pgid, sig) -> None:
        if not pgid:
            return
        try:
            os.killpg(int(pgid), sig)
        except ProcessLookupError:
            pass

    def _check_timeout(self, rt, att) -> None:
        key = rt['key']
        timeout = self.plan['builders'][self._plan_task(key)['builder']]['timeout_s']
        if db.now() - att['started_at'] <= timeout:
            return
        # ponytail: the pgid is checked live (lock held) right before the signal; a reused pgid in that window is a race
        self._kill(key, att['attempt'], self._pgid(att))
        self._event('builder_killed', key=key, task=rt['task_id'], attempt=att['attempt'], reason='timeout',
                    timeout_s=timeout)

    def _kill(self, key: str, n: int, pgid) -> None:
        self._signal(pgid, signal.SIGTERM)
        deadline = time.monotonic() + KILL_GRACE_S
        while self._attempt_live(key, n) and time.monotonic() < deadline:
            time.sleep(0.05)
        if self._attempt_live(key, n):
            self._signal(pgid, signal.SIGKILL)
            deadline = time.monotonic() + 5.0
            while self._attempt_live(key, n) and time.monotonic() < deadline:
                time.sleep(0.05)

    # -- status (B5.5) -----------------------------------------------------------------------
    def _print_status(self, summary: dict) -> None:
        if self.quiet:
            return
        lines = []
        for r in self._rts():
            task = model.load_task(self.conn, r['task_id'])
            att = self._latest_attempt(r['key'])
            pgid = self._pgid(att) if att is not None and r['status'] == 'building' else None
            note = r['status'] + ((' ' + r['last_error']) if r['last_error'] else '')
            lines.append('%-16s %-9s %-12s %d/%d %-8s %s' % (r['key'], task.id, task.state, r['attempts'], r['rebases'],
                                                            pgid or '-', note))
        now = time.monotonic()
        if lines != self._last_lines or now - self._last_print >= STATUS_S or summary['status'] != 'running':
            print('\n'.join(lines), flush=True)
            self._last_lines, self._last_print = lines, now
