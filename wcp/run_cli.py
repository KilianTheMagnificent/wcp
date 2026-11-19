"""`wcp run` argparse surface; cli.build_parser calls register(sub)."""
from __future__ import annotations

import argparse

from . import run
from .db import ProtocolError


def register(sub) -> None:
    sp = sub.add_parser('run', help='lead loop: plan -> builders -> verified publishes; pauses, never cancels')
    sp.add_argument('plan', nargs='?', metavar='PLAN', help='plan.json (see docs/LEAD.md)')
    sp.add_argument('--resume', metavar='RUN_ID', help='reconcile and continue an existing run')
    sp.add_argument('--retry', action='append', metavar='KEY', help='with --resume: retry this paused task')
    sp.add_argument('--retry-all', action='store_true', help='with --resume: retry every paused task')
    sp.add_argument('--plan', dest='plan_opt', metavar='PLAN', help='with --resume: plan that may add new tasks')
    sp.add_argument('--max-iterations', type=int, default=None, metavar='N', help='stop after N loop iterations')
    sp.add_argument('--quiet', action='store_true', help='no status lines')
    sp.add_argument('--json', action='store_true', default=argparse.SUPPRESS, help='print the final summary only')
    sp.add_argument('--repo', default=argparse.SUPPRESS, help='canonical checkout (default: cwd)')
    sp.set_defaults(func=cmd_run)


def cmd_run(ns) -> int:
    from . import cli  # cli imports this module; resolve at call time
    root, conn = cli._open(ns)
    as_json = bool(getattr(ns, 'json', False))
    quiet = ns.quiet or as_json
    if ns.resume:
        if ns.plan:
            raise ProtocolError('usage', 'give either PLAN or --resume RUN_ID, not both', {})
        plan = run.load_plan(ns.plan_opt) if ns.plan_opt else None
        runner = run.Runner(conn, root, plan=plan, run_id=ns.resume, quiet=quiet, max_iterations=ns.max_iterations)
        runner.resume(ns.retry or (), ns.retry_all)
    else:
        if not ns.plan:
            raise ProtocolError('usage', 'PLAN is required (or --resume RUN_ID)', {})
        if ns.retry or ns.retry_all or ns.plan_opt:
            raise ProtocolError('usage', '--retry/--retry-all/--plan need --resume', {})
        runner = run.Runner(conn, root, plan=run.load_plan(ns.plan), quiet=quiet, max_iterations=ns.max_iterations)
        runner.start()
    summary = runner.loop()
    cli._emit(as_json, summary)
    return 0 if summary['status'] == 'done' else 3
