# Acting as the lead (`wcp run`)

You are a Claude or Codex session leading a build. You do not build and you never edit the
canonical checkout. You write a plan, start the run loop, and handle whatever it pauses. The
loop (`wcp run`) is code: it creates tasks, claims them as their scopes free up, launches one
builder process per task, keeps leases alive, submits, verifies and publishes, rebases and
re-dispatches on conflict, auto-approves non-overlapping scope when the plan sets
`"auto_approve_scope": true` (off by default), and **pauses** — never cancels —
anything it cannot resolve. Only you cancel, and only you talk to the human.

## 1. Decompose

- One task per **disjoint write scope**. Overlap is decided conservatively by literal prefix:
  `src/*.ts` and `src/*.css` overlap, `src/**` overlaps everything under `src/`; two tasks may
  both name `tests/**` only if one of them owns it. Two overlapping tasks do not fail — they
  serialize, the second waiting until the first is accepted — so overlap costs parallelism, not
  correctness. Split by directory or file, not by concern.
- Name shared **resources** (`port:5199`, `db:app.sqlite`) so two tasks never hold one at once.
- Order with `depends` (keys); a task is admitted only when every dependency is accepted.
- Choose a **builder** per task. Builders inherit your environment minus every `WCP_*` variable
  and run with `WCP_ROLE=builder`, `WCP_TASK`, `WCP_OWNER`, `WCP_EPOCH`, `WCP_WORKTREE`, `WCP_KEY`.
- Put builder-side checks in `check`. They go into the brief ("run these before finishing") and
  are **never executed by the runner**; publication relies only on the repository's `wcp init`
  checks, which a run requires (a repository with none refuses to start).
- Optional `brief` text is appended verbatim to the generated brief.

## 2. Write `plan.json` and run

```json
{"version": 1, "max_concurrent": 3, "max_rebase": 2, "max_attempts": 2, "auto_approve_scope": true,
 "lease_s": 900,
 "builders": {"claude": {"argv": ["claude", "-p", "{brief}"], "timeout_s": 1800},
              "codex":  {"argv": ["codex", "exec", "-C", "{worktree}", "-s", "workspace-write", "{brief}"], "timeout_s": 1800}},
 "tasks": [{"key": "engine", "goal": "…", "write": ["src/engine/**", "tests/engine/**"], "resource": [],
            "depends": [], "check": ["python3 -m unittest tests.engine"], "builder": "claude"}]}
```

`wcp run plan.json` validates the whole plan first (unknown keys, unknown builders or
dependencies, duplicate keys, credential-shaped or unsafe write patterns all refuse with exit 2
and nothing is created), creates every task in one transaction, then loops. Placeholders in
`argv`: `{brief}`, `{worktree}`, `{task}`, `{owner}`, `{epoch}`, `{key}`. Exit 0 = every task
accepted; exit 3 = paused. `--json` prints the summary object only; `--quiet` drops the status
lines. State: `WCP_HOME/repos/<id>/runs/<run_id>/` holds `run.jsonl` (events: paths and codes,
never output), one brief and one `0600` log per attempt.

## 3. Read the paused list

Each paused task carries `last_error`:

| reason | meaning | your move |
|---|---|---|
| `builder_failed` | exited non-zero, edited nothing, ignored a rebase obligation, or failed the integrated check `max_attempts` times | read `builder-<key>-<n>.log`, fix the goal/brief or scope, retry |
| `builder_lost` | the attempt wrapper never started, twice in a row (no log, no start record) | check that the builder command exists and the run directory is writable, then retry |
| `conflict_exhausted` | rebased `max_rebase` times and still conflicts with what landed | rebase by hand in the retained worktree, or split the task |
| `scope_pending` | wrote outside its scope (or asked for scope) and the path overlaps an active task, or `auto_approve_scope` is off | `wcp scope approve <task> <req>` once the other task is accepted, then retry; or split |
| `partial_publish` | an integrator died mid-publish and a canonical path is in neither before nor after state (recorded by the run, or by a `wcp task verify` run by hand) | **never auto-retried**: inspect the listed paths against the bundle, then `wcp task verify <task>` by hand — escalate |
| `cancelled` | someone ran `wcp task cancel` (a live builder is killed) | nothing; re-plan if the work is still wanted |
| `not_owner` / `stale_epoch` | someone else reclaimed the task (its lease must have lapsed while no runner was alive) | look at `wcp task show <task>`; cancel or re-plan — the runner never takes a task back from a stranger |
| any other code | the catch-all: `sensitive_path`, `unsupported_path`, an unexpected exception, … | the worktree is intact; read the code, fix the cause, retry |

For every paused task choose exactly one of four moves:

1. **Retry**: `wcp run --resume <run_id> --retry <key>` (`--retry-all` for all; counters reset).
   A live lease re-dispatches in the same worktree; an expired one is reclaimed with the delta
   transferred and obligations recorded; a conflicted task is rebased first.
2. **Rebase by hand**: edit the retained worktree (`wcp task show <task>` names it and the retired
   one holding the earlier version), resolve each obligation or `wcp task ack <task> --owner
   run:<run_id>:<key> --epoch <epoch> --keep <path>`, then retry.
3. **Split**: write a new plan that keeps the existing keys byte-for-byte and adds narrower tasks;
   `wcp run --resume <run_id> --plan new.json`.
4. **Cancel**: `wcp task cancel <task>`. You are the only party that cancels.

Every `--resume` reconciles from durable state: live builders are adopted (never re-dispatched
over, a retried row's included), exited ones are reaped, submitted ones verified, a claim or rebase
the previous runner died inside is re-provisioned with the retired worktree's delta carried over,
and the run's own lapsed leases are renewed rather than paused — so resuming hours later still
finishes without re-running a builder that had finished. A second runner on the same run refuses
`lock_held`, and the run never reports itself paused while one of its builders is alive. Escalate to
the human only when none of the four moves resolves a task, or when the reason is `partial_publish`.
