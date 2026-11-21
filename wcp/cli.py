"""argparse surface: human text by default, --json for agents; exit 0 ok, 2 usage, 3 protocol refusal, 1 unexpected."""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shlex
import sys
import traceback
from pathlib import Path

from . import __version__, db, gitx, integrate, model, paths, recover, run_cli, tasks
from .db import ProtocolError

LINK_CAVEAT = ('linked directories are shared, writable state reachable from every builder and every check; '
               'nothing under them is covered by the canonical-untouched guarantee')


def _jsonable(o):
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    return str(o)


def _plain(o):
    return json.loads(json.dumps(o, default=_jsonable))


def _human(obj, indent=0, out=None):
    out = out or sys.stdout
    pad = '  ' * indent
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                out.write('%s%s:\n' % (pad, k))
                _human(v, indent + 1, out)
            else:
                out.write('%s%s: %s\n' % (pad, k, '' if v is None else v))
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, (dict, list)):
                out.write('%s-\n' % pad)
                _human(v, indent + 1, out)
            else:
                out.write('%s- %s\n' % (pad, v))
    else:
        out.write('%s%s\n' % (pad, obj))


def _emit(as_json: bool, result: dict) -> None:
    result = _plain(result)
    if as_json:
        print(json.dumps(dict({'ok': True}, **result), sort_keys=True))
    else:
        _human(result)


def _emit_error(as_json: bool, code: str, message: str, detail: dict) -> None:
    if as_json:
        print(json.dumps({'ok': False, 'code': code, 'error': message, 'detail': _plain(detail)}, sort_keys=True))
    else:
        sys.stderr.write('wcp: %s: %s\n' % (code, message))
        if detail:
            _human(_plain(detail), 1, sys.stderr)


def _root(ns) -> Path:
    return gitx.canonical_root(Path(getattr(ns, 'repo', None) or os.getcwd()))


def _open(ns, read_only: bool = False):
    root = _root(ns)
    conn = db.open_db(root, read_only=read_only)
    conns = getattr(ns, '_conns', None)
    if conns is not None:
        conns.append(conn)
    return root, conn


def _task_view(conn, task: model.Task) -> dict:
    d = dataclasses.asdict(task)
    d['scope'] = model.load_scopes(conn, task.id)
    d['depends'] = model.load_deps(conn, task.id)
    d['checks'] = model.load_checks(conn, task.id)
    d['worktrees'] = [dataclasses.asdict(w) for w in model.load_worktrees(conn, task.id)]
    sub = tasks.latest_submission(conn, task.id)
    d['submission'] = None if sub is None else {'id': sub.id, 'epoch': sub.epoch, 'worktree': sub.worktree_path,
                                                'changes': sub.manifest()['changes'],
                                                'receipts': tasks.receipts_for(conn, sub.id)}
    d['scope_requests'] = [dict(r) for r in conn.execute(
        'SELECT * FROM scope_requests WHERE task_id=? ORDER BY id', (task.id,))]
    d['rebase_obligations'] = [dataclasses.asdict(o) for o in model.load_obligations(conn, task.id, task.epoch)]
    return d


def _wcp_cmd() -> str:
    launcher = Path(__file__).resolve().parent.parent / 'bin' / 'wcp'
    return shlex.quote(str(launcher)) if launcher.is_file() else '%s -m wcp' % shlex.quote(sys.executable)


# ---------------------------------------------------------------- commands

def cmd_init(ns):
    root, conn = _open(ns)
    cfg = gitx.read_agent_tools_config(root)
    with db.write_txn(conn):
        db.meta_set(conn, 'root', str(root))
        if ns.check:
            checks = [{'argv': None, 'cmd': c, 'cwd': '.', 'timeout_s': 900} for c in ns.check]
        elif cfg is not None:
            checks = cfg['checks']
        else:
            checks = tasks.required_checks(conn)
        db.meta_set(conn, 'required_checks', json.dumps(checks))
        if ns.no_link:
            links = []
        elif ns.link:
            links = [paths.normalize_repo_path(d) for d in ns.link]
        else:
            links = tasks.link_dirs(conn)
        db.meta_set(conn, 'link_dirs', json.dumps(links))
        preserve = cfg['preserve_roots'] if cfg is not None else []
        db.meta_set(conn, 'preserve_roots', json.dumps(preserve))
    out = {'root': str(root), 'repo_id': db.repo_id(root), 'state_dir': str(db.repo_dir(root)),
           'required_checks': checks, 'link_dirs': links, 'preserve_roots': preserve}
    if links:
        out['link_caveat'] = LINK_CAVEAT
    return out


def cmd_version(ns):
    out = {'version': __version__, 'python': '%d.%d.%d' % sys.version_info[:3]}
    if getattr(ns, 'json', False):
        return out
    print('wcp %(version)s (python %(python)s)' % out)
    return {}


def _lease_text(task: model.Task, at: float) -> str:
    if task.lease_expires_at is None:
        return '-'
    return 'expired' if task.lease_expired(at) else '%ds' % int(task.lease_expires_at - at)


def cmd_status(ns):
    root, conn = _open(ns, read_only=True)
    at = db.now()
    lock = conn.execute("SELECT holder, acquired_at FROM locks WHERE name='integrator'").fetchone()
    links = tasks.link_dirs(conn)
    return {'root': str(root), 'repo_id': db.repo_id(root), 'state_dir': str(db.repo_dir(root)),
            'head': gitx.head_revision(root), 'dirty': gitx.is_dirty(root),
            'integrator': None if lock is None else dict(lock),
            'required_checks': tasks.required_checks(conn), 'link_dirs': links,
            'link_caveat': LINK_CAVEAT if links else None,
            'preserve_roots': json.loads(db.meta_get(conn, 'preserve_roots', '[]') or '[]'),
            'epoch': int(db.meta_get(conn, 'epoch', '0')),
            'active_tasks': [{'id': t.id, 'state': t.state, 'owner': t.owner, 'epoch': t.epoch,
                              'lease': _lease_text(t, at), 'worktree': t.worktree_path,
                              'scope': model.load_scopes(conn, t.id)} for t in tasks.active_tasks(conn, at)]}


def cmd_ps(ns):
    root, conn = _open(ns, read_only=True)
    at = db.now()
    rows = []
    for r in conn.execute('SELECT * FROM tasks ORDER BY created_at, id'):
        t = model.Task.from_row(r)
        if not ns.all and t.state in model.TERMINAL_STATES:
            continue
        sc = model.load_scopes(conn, t.id)
        rows.append({'id': t.id, 'state': t.state, 'owner': t.owner or '-', 'epoch': t.epoch if t.epoch is not None else '-',
                     'lease': _lease_text(t, at), 'scope': ' '.join(sc['path'] + sc['resource']) or '-',
                     'goal': t.goal})
    if getattr(ns, 'json', False):
        return {'tasks': rows}
    cols = ('id', 'state', 'owner', 'epoch', 'lease', 'scope')
    widths = {c: max([len(c)] + [len(str(r[c])) for r in rows]) for c in cols}
    print('  '.join(c.upper().ljust(widths[c]) for c in cols))
    for r in rows:
        print('  '.join(str(r[c]).ljust(widths[c]) for c in cols))
    return {}


def cmd_task_create(ns):
    root, conn = _open(ns)
    task = tasks.create_task(conn, ns.goal, ns.write or [], ns.resource or [], ns.depends or [], ns.check or [])
    return {'task': _task_view(conn, task), 'id': task.id, 'state': task.state}


def cmd_task_ready(ns):
    root, conn = _open(ns)
    task = tasks.ready_task(conn, ns.id)
    return {'id': task.id, 'state': task.state, 'version': task.version,
            'waiting_on': tasks.waiting_on(conn, task.id, db.now())}


def _claim_view(out, task_id):
    out['task'] = dataclasses.asdict(out['task'])
    out['id'] = task_id
    out['state'] = out['task']['state']
    return out


def cmd_task_claim(ns):
    root, conn = _open(ns)
    return _claim_view(tasks.claim_task(conn, root, ns.id, ns.owner, ns.lease, wait_s=ns.wait), ns.id)


def cmd_task_rebase(ns):
    root, conn = _open(ns)
    return _claim_view(tasks.rebase_task(conn, root, ns.id, ns.owner, ns.epoch, ns.lease), ns.id)


def cmd_task_ack(ns):
    root, conn = _open(ns)
    tasks.ack_obligation(conn, ns.id, ns.owner, ns.epoch, ns.keep)
    pending = [o.path for o in model.load_obligations(conn, ns.id, ns.epoch) if o.status == 'pending']
    return {'id': ns.id, 'epoch': ns.epoch, 'kept': ns.keep, 'pending': pending}


def cmd_task_heartbeat(ns):
    root, conn = _open(ns)
    task = tasks.heartbeat(conn, ns.id, ns.owner, ns.epoch, ns.lease, ns.expect_version)
    return {'id': task.id, 'state': task.state, 'epoch': task.epoch, 'version': task.version,
            'lease_expires_at': task.lease_expires_at}


def cmd_task_submit(ns):
    root, conn = _open(ns)
    out = tasks.submit(conn, root, ns.id, ns.owner, ns.epoch, ns.record_failing, ns.expect_version)
    out['id'] = ns.id
    return out


def cmd_task_verify(ns):
    root, conn = _open(ns)
    dry = getattr(ns, 'dry_run', False)
    holder = getattr(ns, 'holder', None) or '%s:%d' % (os.environ.get('USER', 'integrator'), os.getpid())
    out = integrate.verify(conn, root, ns.id, holder, dry_run=dry, wait_s=ns.wait)
    out['id'] = ns.id
    return out


def cmd_task_reject(ns):
    root, conn = _open(ns)
    task = integrate.reject(conn, ns.id, ns.reason)
    return {'id': task.id, 'state': task.state, 'version': task.version}


def cmd_task_block(ns):
    root, conn = _open(ns)
    task = tasks.block(conn, ns.id, ns.owner, ns.epoch, ns.reason, ns.expect_version)
    return {'id': task.id, 'state': task.state, 'version': task.version, 'blocked_reason': task.blocked_reason}


def cmd_task_abandon(ns):
    root, conn = _open(ns)
    task = tasks.abandon(conn, ns.id, ns.owner, ns.epoch, ns.expect_version)
    return {'id': task.id, 'state': task.state, 'version': task.version}


def cmd_task_cancel(ns):
    root, conn = _open(ns)
    task = tasks.cancel(conn, ns.id)
    return {'id': task.id, 'state': task.state, 'version': task.version}


def cmd_task_release(ns):
    root, conn = _open(ns)
    task = tasks.release(conn, ns.id)
    return {'id': task.id, 'state': task.state, 'epoch': task.epoch, 'version': task.version,
            'lease_expires_at': task.lease_expires_at}


def cmd_task_recover(ns):
    root, conn = _open(ns)
    out = recover.recover(conn, root, ns.id, db.now())
    if not getattr(ns, 'json', False):
        nxt = out.pop('next_action')
        _human(_plain(out))
        print('next_action: %s' % nxt)
        return {}
    return out


def cmd_task_cleanup(ns):
    root, conn = _open(ns)
    removed = tasks.cleanup(conn, root, ns.id)
    return {'id': ns.id, 'removed': [str(p) for p in removed]}


def cmd_task_show(ns):
    root, conn = _open(ns, read_only=True)
    return _task_view(conn, model.load_task(conn, ns.id))


def cmd_events(ns):
    root, conn = _open(ns, read_only=True)
    model.load_task(conn, ns.id)
    return {'id': ns.id, 'events': [
        {'id': r['id'], 'at': r['at'], 'kind': r['kind'], 'detail': json.loads(r['detail_json'] or '{}')}
        for r in conn.execute('SELECT * FROM events WHERE task_id=? ORDER BY id', (ns.id,))]}


def cmd_scope_request(ns):
    root, conn = _open(ns)
    kind, pattern = ('path', ns.write) if ns.write else ('resource', ns.resource)
    req = tasks.request_scope(conn, ns.id, ns.owner, ns.epoch, kind, pattern, ns.reason)
    return {'id': ns.id, 'request_id': req, 'kind': kind, 'pattern': pattern, 'status': 'pending'}


def cmd_scope_approve(ns):
    root, conn = _open(ns)
    req = tasks.approve_scope(conn, ns.id, ns.req)
    return {'id': ns.id, 'request_id': req.id, 'kind': req.kind, 'pattern': req.pattern, 'status': req.status,
            'scope': model.load_scopes(conn, ns.id)}


def cmd_scope_deny(ns):
    root, conn = _open(ns)
    req = tasks.deny_scope(conn, ns.id, ns.req, ns.reason)
    return {'id': ns.id, 'request_id': req.id, 'kind': req.kind, 'pattern': req.pattern, 'status': req.status}


def cmd_hook_pretooluse(ns):
    from . import hooks
    return hooks.main_pretooluse(sys.stdin.read())


HOOK_DIALECTS = ('claude', 'codex', 'grok', 'qwen')


def _hook_dialect(ns):
    chosen = [d for d in HOOK_DIALECTS if getattr(ns, d, False)]
    if len(chosen) != 1:
        raise ProtocolError('bad_state', 'choose exactly one of %s' % ', '.join('--' + d for d in HOOK_DIALECTS), {})
    return chosen[0]


def cmd_hook_print(ns):
    from . import hooks
    print(json.dumps(hooks.settings_snippet(_hook_dialect(ns), _wcp_cmd()), indent=2))
    return {}


def cmd_hook_install(ns):
    from . import hooks
    dialect = _hook_dialect(ns)
    return {'dialect': dialect, 'installed': str(hooks.install(dialect, Path(ns.into), _wcp_cmd()))}


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--json', action='store_true', default=argparse.SUPPRESS, help='machine-readable output')
    common.add_argument('--repo', default=argparse.SUPPRESS, help='canonical checkout (default: cwd)')
    p = argparse.ArgumentParser(prog='wcp', parents=[common],
                                description='Workspace Coordination Protocol v0.1 reference CLI')
    sub = p.add_subparsers(dest='cmd', metavar='COMMAND')
    sub.required = True

    def add(parent, name, func, **kw):
        sp = parent.add_parser(name, parents=[common], **kw)
        sp.set_defaults(func=func)
        return sp

    def owner_args(sp, epoch=True):
        sp.add_argument('--owner', required=True)
        if epoch:
            sp.add_argument('--epoch', required=True, type=int)

    sp = add(sub, 'init', cmd_init, help='register repo, checks, link dirs (opt-in)')
    sp.add_argument('--check', action='append', metavar='CMD', help='required check (shell); repeatable')
    sp.add_argument('--link', action='append', metavar='DIR', help='git-ignored dir to symlink into worktrees')
    sp.add_argument('--no-link', action='store_true', help='clear link dirs')
    add(sub, 'version', cmd_version, help='wcp and python versions (no repo required)')
    add(sub, 'status', cmd_status, help='repo, lock holder, active tasks')
    sp = add(sub, 'ps', cmd_ps, help='tasks table')
    sp.add_argument('--all', action='store_true', help='include terminal tasks')

    task = sub.add_parser('task', help='task operations').add_subparsers(dest='task_cmd', metavar='OP')
    task.required = True
    sp = add(task, 'create', cmd_task_create, help='propose a task')
    sp.add_argument('--goal', required=True)
    sp.add_argument('--write', action='append', metavar='PAT')
    sp.add_argument('--resource', action='append', metavar='R')
    sp.add_argument('--depends', action='append', metavar='ID')
    sp.add_argument('--check', action='append', metavar='CMD',
                    help='acceptance check run at submit in the builder worktree; never run by the integrator')
    for name, func, hlp in (('ready', cmd_task_ready, 'coordinator admits (independence + deps exist)'),
                            ('cancel', cmd_task_cancel, 'coordinator cancels'),
                            ('release', cmd_task_release, 'coordinator force-expires the lease (new epoch)'),
                            ('recover', cmd_task_recover, 'durable state -> next safe action'),
                            ('cleanup', cmd_task_cleanup, 'non-destructive worktree removal'),
                            ('show', cmd_task_show, 'task record')):
        add(task, name, func, help=hlp).add_argument('id')
    sp = add(task, 'claim', cmd_task_claim, help='atomic claim + worktree')
    sp.add_argument('id')
    owner_args(sp, epoch=False)
    sp.add_argument('--lease', type=int, default=900, metavar='SECONDS')
    sp.add_argument('--wait', type=float, default=0, metavar='SECONDS',
                    help='poll until the overlap/deps clear (-1: no limit); default 0 refuses immediately')
    sp = add(task, 'rebase', cmd_task_rebase, help="owner's own reclaim from current canonical (new epoch)")
    sp.add_argument('id')
    owner_args(sp)
    sp.add_argument('--lease', type=int, default=900, metavar='SECONDS')
    sp = add(task, 'ack', cmd_task_ack, help='owner keeps canonical\'s version of a rebase obligation')
    sp.add_argument('id')
    owner_args(sp)
    sp.add_argument('--keep', required=True, metavar='PATH')
    sp = add(task, 'heartbeat', cmd_task_heartbeat, help='renew lease; claimed->running')
    sp.add_argument('id')
    owner_args(sp)
    sp.add_argument('--lease', type=int, default=900, metavar='SECONDS')
    sp.add_argument('--expect-version', type=int, default=None)
    sp = add(task, 'submit', cmd_task_submit, help='run checks in the worktree, store bundle')
    sp.add_argument('id')
    owner_args(sp)
    sp.add_argument('--record-failing', action='store_true')
    sp.add_argument('--expect-version', type=int, default=None)
    sp = add(task, 'verify', cmd_task_verify, help='integrator: gates + integrated checks + publish')
    sp.add_argument('id')
    sp.add_argument('--dry-run', action='store_true')
    sp.add_argument('--holder', default=None)
    sp.add_argument('--wait', type=float, default=0, metavar='SECONDS', help='poll while the lock is held (-1: no limit)')
    sp = add(task, 'accept', cmd_task_verify, help='alias of verify (always re-verifies)')
    sp.add_argument('id')
    sp.add_argument('--holder', default=None)
    sp.add_argument('--wait', type=float, default=0, metavar='SECONDS', help='poll while the lock is held (-1: no limit)')
    sp = add(task, 'reject', cmd_task_reject, help='integrator: back to owner')
    sp.add_argument('id')
    sp.add_argument('--reason', required=True)
    sp = add(task, 'block', cmd_task_block, help='owner marks the task blocked')
    sp.add_argument('id')
    owner_args(sp)
    sp.add_argument('--reason', required=True)
    sp.add_argument('--expect-version', type=int, default=None)
    sp = add(task, 'abandon', cmd_task_abandon, help='owner gives the task up')
    sp.add_argument('id')
    owner_args(sp)
    sp.add_argument('--expect-version', type=int, default=None)

    sp = add(sub, 'events', cmd_events, help='audit trail for a task')
    sp.add_argument('id')

    sc = sub.add_parser('scope', help='scope requests').add_subparsers(dest='scope_cmd', metavar='OP')
    sc.required = True
    sp = add(sc, 'request', cmd_scope_request, help='owner asks for more scope')
    sp.add_argument('id')
    owner_args(sp)
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument('--write', metavar='PAT')
    g.add_argument('--resource', metavar='R')
    sp.add_argument('--reason', required=True)
    sp = add(sc, 'approve', cmd_scope_approve, help='coordinator approves (overlap-checked)')
    sp.add_argument('id')
    sp.add_argument('req', type=int)
    sp = add(sc, 'deny', cmd_scope_deny, help='coordinator denies')
    sp.add_argument('id')
    sp.add_argument('req', type=int)
    sp.add_argument('--reason', required=True)

    hk = sub.add_parser('hook', help='PreToolUse hook').add_subparsers(dest='hook_cmd', metavar='OP')
    hk.required = True
    add(hk, 'pretooluse', cmd_hook_pretooluse, help='read the PreToolUse payload on stdin; exit 2 denies')
    add(hk, 'claude', cmd_hook_pretooluse, help='alias of pretooluse')
    for name, func, help_ in (('print', cmd_hook_print, 'print the settings block for a dialect'),
                              ('install', cmd_hook_install, 'merge the block into a hook or settings file')):
        sp = add(hk, name, func, help=help_)
        for d in HOOK_DIALECTS:
            sp.add_argument('--' + d, action='store_true')
        if name == 'install':
            sp.add_argument('--into', required=True, metavar='PATH')
    run_cli.register(sub)  # `wcp run` (addendum Part B)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    try:
        ns = parser.parse_args(argv)
    except SystemExit as e:  # argparse: 0 for --help, 2 for usage
        return 0 if e.code in (None, 0) else 2
    as_json = bool(getattr(ns, 'json', False))
    ns._conns = []
    try:
        if hasattr(ns, 'owner'):
            tasks.validate_owner(ns.owner)
        result = ns.func(ns)
        if isinstance(result, int):
            return result
        if result or as_json:
            _emit(as_json, result or {})
        return 0
    except ProtocolError as e:
        _emit_error(as_json, e.code, e.message, e.detail)
        return 2 if e.code == 'usage' else 3
    except gitx.GitError as e:
        _emit_error(as_json, 'git_error', str(e), {})
        return 1
    except Exception as e:  # unexpected
        if os.environ.get('WCP_DEBUG'):
            traceback.print_exc()
        _emit_error(as_json, 'internal', '%s: %s' % (type(e).__name__, e), {})
        return 1
    finally:
        for conn in ns._conns:
            conn.close()
