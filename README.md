# wcp — Workspace Coordination Protocol

`wcp` is a zero-dependency Python 3.9+ CLI that lets multiple coding agents work concurrently in the same Git repository without overwriting, invalidating, or silently absorbing each other's changes.

- **One canonical checkout, one writer:** builders work only inside isolated detached git worktrees; only verified publishes touch the canonical checkout.
- **Atomic claims with leases and fencing epochs:** expired leases can be reclaimed into a fresh worktree under a new epoch, and stale epochs are refused even if their diff would apply.
- **Declared write scopes and resources:** overlapping scopes serialize cleanly at claim time (with optional `--wait` polling) rather than colliding silently.
- **Hash-bound change bundles:** submissions record before/after SHA-256 hashes; integration re-runs the repository's declared checks against a fresh candidate built from current canonical before publishing.
- **Opt-in, local-only, standard library only:** requires Python 3.9+ and Git, installs nothing globally, writes nothing inside the target repository, and makes no network calls.

## Requirements

- Python 3.9 or newer (standard library only — no third-party packages)
- Git (with `git worktree` support)
- POSIX environment (macOS or Linux; uses `fcntl.flock` advisory locks)

## Install

Run directly without installing using [`uvx`](https://docs.astral.sh/uv/), or install onto your `PATH` with [`pipx`](https://pipx.pypa.io/):

```sh
# Try without installing
uvx --from git+https://github.com/KilianTheMagnificent/wcp wcp version

# Or install globally in an isolated environment
pipx install git+https://github.com/KilianTheMagnificent/wcp
wcp version
```

Or clone the repository and run the thin launcher directly, or add `bin/` to your `PATH`:

```sh
git clone https://github.com/KilianTheMagnificent/wcp.git
cd wcp
./bin/wcp version
```

To make `./bin/wcp` available from any repository:

```sh
export PATH="$PWD/bin:$PATH"
wcp version
```

You can also invoke `python3 -m wcp` with the repository root on `PYTHONPATH`.

Add `--json` to any command for a single JSON object per invocation (`{"ok": true, ...}` or `{"ok": false, "code": ..., "error": ...}`). Exit codes: `0` ok, `2` usage error, `3` typed protocol refusal, `1` unexpected error.

## Quickstart

Run these commands inside a target Git repository once `wcp` is on your `PATH`:

```sh
# 1. Register the repository and its required verification check (once per repo)
wcp init --check "python3 -m unittest discover -s tests"

# 2. Coordinator creates and readies a task with a declared write scope
wcp task create --goal "update engine timing" --write "src/engine/**" --resource port:5199
wcp task ready <task-id>

# 3. Builder claims the task (prints worktree path, epoch, lease, and sandbox hint)
wcp task claim <task-id> --owner claude:session-a1b2
# Optional: poll until overlapping tasks finish (N seconds, or -1 to wait indefinitely)
# wcp task claim <task-id> --owner claude:session-a1b2 --wait -1

# ... builder edits files ONLY inside the printed worktree path ...

# 4. Builder renews its lease as needed and submits a hash-bound bundle
wcp task heartbeat <task-id> --owner claude:session-a1b2 --epoch 1
wcp task submit <task-id> --owner claude:session-a1b2 --epoch 1

# 5. Integrator verifies against current canonical and publishes atomically
wcp task verify <task-id>

# 6. Non-destructive cleanup once accepted (refuses if any file is unaccounted for)
wcp task cleanup <task-id>
```

Additional everyday commands:

```sh
wcp task rebase <task-id> --owner <you> --epoch 1                # canonical moved: fresh worktree, new epoch, delta transferred
wcp task ack <task-id> --owner <you> --epoch 2 --keep src/x.py   # keep canonical's version for a rebase obligation
wcp task verify <task-id> --wait 60                              # poll while another integrator holds the lock
wcp task verify <task-id> --dry-run                              # run gates and checks without writing canonical
wcp task recover <task-id>                                       # inspect durable state -> single next safe action
wcp status                                                       # repository state, lock holder, active tasks
wcp ps --all                                                     # task table including terminal tasks
wcp task show <task-id>                                          # full task record, scopes, worktrees, receipts
wcp events <task-id>                                             # ordered audit log for a task
```

**Roles:**
- **Coordinator:** `task create`, `task ready`, `task cancel`, `task release` (force-expires a lease; the new epoch fences the previous owner), `scope approve|deny`.
- **Worker:** commands carry `--owner` and `--epoch` and fail with `stale_epoch` if the task was reclaimed or released.
- **Integrator:** `task verify` / `task accept` (alias; always re-verifies against current canonical) and `task reject --reason`.

If a repository includes an optional `.agent-tools.json` check manifest and `wcp init` is run without `--check`, required checks and `preserve_roots` are read from that file.

## Running as lead (`wcp run`)

`wcp run plan.json` provides a deterministic lead loop for coordinating multiple builder processes from a single plan file. It creates every task in one transaction (nothing is created if validation fails), claims tasks as their scopes clear, launches one builder process per task, renews leases from a dedicated thread, submits on the builder's behalf, verifies and publishes, rebases and re-dispatches on conflict, approves non-overlapping scope requests when enabled, and **pauses** — never cancels — anything it cannot resolve automatically.

See [docs/LEAD.md](docs/LEAD.md) for the full lead playbook.

Example `plan.json`:

```json
{
  "version": 1,
  "max_concurrent": 2,
  "max_attempts": 2,
  "max_rebase": 2,
  "auto_approve_scope": true,
  "builders": {
    "codex": {
      "argv": ["codex", "exec", "-C", "{worktree}", "-s", "workspace-write", "{brief}"],
      "timeout_s": 1800
    },
    "claude": {
      "argv": ["claude", "-p", "{brief}"],
      "timeout_s": 1800
    }
  },
  "tasks": [
    {
      "key": "engine",
      "goal": "make the engine tick",
      "write": ["src/engine/**"],
      "builder": "codex",
      "check": ["python3 -m unittest tests.engine"]
    },
    {
      "key": "docs",
      "goal": "document engine timing",
      "write": ["docs/engine.md"],
      "depends": ["engine"],
      "builder": "claude"
    }
  ]
}
```

```sh
wcp run plan.json                         # exit 0: every task accepted; exit 3: paused (with reasons)
wcp run --resume r-1a2b3c                 # reconcile and continue after a crash or Ctrl-C (adopts live builders)
wcp run --resume r-1a2b3c --retry docs    # retry one paused task (--retry-all for all; --plan adds tasks)
```

A **paused** task retains its worktree, lease, and every attempt log; nothing is deleted or cancelled. Paused reasons include `builder_failed`, `builder_lost`, `conflict_exhausted`, `scope_pending`, `partial_publish` (never auto-retried), or any other recorded protocol refusal code.

Key `wcp run` safeguards:
- The repository must have checks configured via `wcp init`; a zero-check repository cannot start a run. Plan `check` entries are builder-side brief instructions and are never executed by the runner.
- Builders receive a minimal allowlisted environment (standard shell/locale vars, proxy/TLS/virtualenv vars, and AI provider API keys `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `XAI_API_KEY`, plus any explicit per-builder `env_passthrough` entries) with all inherited `WCP_*` variables, loader hooks, and ambient non-allowlisted secrets stripped, then receive only `WCP_HOME`, `WCP_TASK`, `WCP_OWNER`, `WCP_EPOCH`, `WCP_WORKTREE`, `WCP_KEY`, and `WCP_ROLE=builder`.
- Each attempt runs in its own process group under `wcp/_attempt.py` (invoked by absolute path with `python -I`, preventing a `wcp/` package inside the worktree from shadowing it), holds a per-attempt flock, and writes a `0600` redacted log (`builder-<key>-<n>.log`, capped at 1 MiB). Heuristic redaction covers common token patterns before marker extraction, but logs should still be treated as sensitive. Timeouts terminate the entire process group (`SIGTERM`, then `SIGKILL`).
- Process output is never stored in SQLite, `run.jsonl`, or briefs; re-dispatch briefs include only the failed check command and exit code.
- Builders may print `SCOPE-NEEDED: <path>` (exit 0, no edits) to request scope expansion, or `KEEP-CANONICAL: <path>` to accept canonical's version for a rebase obligation.

## Agent instructions snippet

You can paste the following block into your target repository's agent instructions file:

```text
This repository is coordinated by wcp. Before writing any file, claim a task:
`wcp task claim <id> --owner <your identity>` (the coordinator creates and readies tasks;
`wcp ps` lists them). An owner prefixed `orch:` or `run:` there is an orchestrated worker
rather than a human terminal session. The claim prints a worktree path, an epoch, and a
lease expiry. Work only inside that worktree; never edit the canonical checkout, and never
edit a worktree that belongs to another task or that wcp reports as retired. Renew the
lease with `wcp task heartbeat <id> --owner <you> --epoch <epoch>` at least every 10 minutes;
an expired lease lets someone else reclaim the task and your epoch stops being accepted.
Write only paths matching the task's declared scope; if you need another path or resource,
run `wcp scope request <id> --owner <you> --epoch <epoch> --write <pattern> --reason <why>`
and wait for approval instead of writing there. When the task's checks pass, run
`wcp task submit <id> --owner <you> --epoch <epoch>`; do not commit, push, or otherwise
run git mutations, and do not run `wcp task verify` or `accept` on your own task — the
integrator does that. If anything looks wrong, `wcp task recover <id>` says what to do next.
```

## Hook installation

`wcp hook pretooluse` is a `PreToolUse` command hook supporting Claude Code, Codex, Grok, and Qwen. It reads the tool call JSON from stdin and either exits `0` silently (allow / no opinion) or exits `2` with a one-line reason on stderr (deny). It never prints tool content.

It denies:
- writes into the canonical checkout while any task is active (override in the integrator shell with `WCP_ROLE=integrator`),
- writes outside the caller's task scope or assigned worktree,
- writes into a retired or provisioning worktree,
- paths containing `..`, `.git`, or credential-shaped filenames,
- writes for tasks whose lease has expired.

Paths outside any `wcp`-registered repository are always allowed. On unexpected internal errors, the hook fails open and appends one line to `WCP_HOME/hook-errors.log`.

Inspect or install a hook block (nothing is modified unless you run `install`; repeated installs are idempotent):

```sh
wcp hook print --claude|--codex|--grok|--qwen
wcp hook install --claude --into ~/.claude/settings.json
wcp hook install --codex  --into ~/.codex/hooks.json
wcp hook install --grok   --into ~/.grok/hooks/wcp.json
wcp hook install --qwen   --into <repo>/.qwen/settings.json
```

Live verification has been performed for Claude Code via the user-level hook; the Codex, Grok, and Qwen blocks follow each CLI's documented hook schema and have full unit-test coverage, with live session testing still in progress.

Project-level hook files are honored only in directories trusted by the host CLI. Hooks are a guard rail rather than a hard security boundary: CLI settings or flags that disable hooks (such as `disableAllHooks`, `--safe-mode`, or hook-trust bypass flags) turn them off, and shell commands (`Bash`) are not scope-enforced by the hook.

## OS sandbox backstop

`wcp task claim` prints a ready-to-run Codex sandbox hint alongside the worktree path:

```sh
codex -C <worktree> -s workspace-write
```

When launched inside an OS-level workspace sandbox, writes outside the assigned worktree — including canonical — are blocked by the operating system rather than by hooks alone.

## State location and permissions

All `wcp` state lives outside the repository under `WCP_HOME`. By default `WCP_HOME` is `~/Library/Application Support/wcp` on macOS and `$XDG_DATA_HOME/wcp` (default `~/.local/share/wcp`) on Linux; you can override it anywhere with `WCP_HOME`.

```text
WCP_HOME/repos/<repo_id>/state.sqlite3               mutable state database (0600)
WCP_HOME/repos/<repo_id>/worktrees/<task>-e<epoch>/  one detached worktree per claim
WCP_HOME/repos/<repo_id>/bundles/<task>/<sub_id>/    manifest.json + blobs/<sha256>
WCP_HOME/repos/<repo_id>/integration/                scratch worktree, removed after verify
WCP_HOME/repos/<repo_id>/integrator.lock             flock held across verify and publish
WCP_HOME/hook-errors.log
```

`repo_id` is the first 16 hex characters of the SHA-256 hash of the canonical root's resolved path. Directories and files under `WCP_HOME` are created `0700`/`0600`. `wcp` writes nothing into the repository itself and never runs `git commit`, `push`, `checkout`, `reset`, `stash`, or `clean` on canonical — humans commit. The only code that writes canonical is the publish step of `verify`, which writes only the bundle's declared paths after re-checking their before-hashes under `integrator.lock`.

## What the guarantee covers, and what it does not

**Covered:** tracked and untracked-non-ignored regular files in the canonical checkout change only through a verified publish whose per-path before-hashes matched at publish time; two active tasks never hold overlapping write scopes; stale epochs are refused even when their diff would apply; worktrees are removed only when every entry is accounted for.

**Not covered:**
- **Git-ignored files** (caches, build outputs, local databases) are neither tracked nor protected.
- **Linked directories (`wcp init --link`)** are symlinked into every worktree as shared writable state reachable by builders and checks. `wcp status` lists them.
- **Declared check commands** run uncontained in the candidate worktree. Receipts carry `"trust": "untrusted_observation"`. Any check that mutates a non-ignored file fails verification (`candidate_mutated`). Only checks declared via `wcp init` (or `.agent-tools.json`) run during `verify`; `task create --check` runs only at `submit` in the builder worktree. Repository git hooks (`post-checkout`, `core.hooksPath`) are suppressed during worktree provisioning.
- **Credential-shaped paths** (`.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa*`, `credentials*.json`, paths under `.ssh`/`.aws`/`.kube`/`.gnupg`/`.docker`, etc.; value-free `.env.example`, `.env.sample`, `.env.template` are exempt) are never read, hashed, copied, or bundled. Tracked credential paths refuse provisioning with `sensitive_path` without override.
- **Hook bypasses or shell writes:** use an OS-level sandbox such as `codex -C <worktree> -s workspace-write` as the mechanical backstop.

If `verify` ever reports `partial_publish`, a previous integrator died mid-publish and one of the listed canonical paths matches neither the bundle's before-hash nor after-hash. Nothing further is written. Compare those paths against `WCP_HOME/repos/<repo_id>/bundles/`, reconcile manually, and re-run `verify`; recovery is idempotent.

## Limitations

- Git repositories only; non-git directories fail with `not_a_repo`.
- `Bash` commands are not scope-enforced by the `PreToolUse` hook.
- Scope overlap uses conservative literal-prefix matching (`src/*.ts` and `src/*.css` overlap), erring toward serialization rather than collision.
- Every `verify` builds a fresh scratch integration worktree from current canonical, applies the bundle, runs `init`-declared checks, and publishes only if checks pass and canonical did not drift.
- `task cleanup` has no `--force`: it refuses if any unaccounted file (including git-ignored files) remains.
- Reclaim after lease expiry (and `task rebase`) transfers edits only for paths whose canonical baseline hash did not move; moved paths become durable rebase obligations that block `submit` (`rebase_pending`) until resolved or acknowledged via `task ack --keep`.
- Overlay replay of a dirty canonical uses `git diff HEAD --binary | git apply` plus untracked regular-file copies; submodule pointers and clean/smudge filters are not replayed.

## Testing

Run the full standard-library test suite from the repository root (every test creates its own isolated temporary repository and `WCP_HOME`):

```sh
python3 -m unittest discover -s tests -v
```

Measured locally on Python 3.9.6: **232 tests passed in 141.99s** (including ten black-box protocol acceptance tests in `tests/test_acceptance.py` driven end-to-end through `python3 -m wcp`).

To run a single module:

```sh
python3 -m unittest tests.test_scope -v
```

## Status & How This Was Built

- **Current status:** `v0.3.0` implementing Workspace Coordination Protocol v0.1. Ready for local multi-agent Git workflows on macOS and Linux. `wcp` is a local developer CLI and does not handle logins, encryption, backups, or passwords.
- **How this was built:** Andre directs design, specifications, and verification with AI coding assistants. Behavior is checked through 232 automated unit and black-box acceptance tests — covering concurrent claim races, stale-epoch fencing, drift detection, crash recovery, partial-publish journaling, sensitive-path exclusion, hook enforcement, and `wcp run` orchestration — which you can verify directly on your own machine with `python3 -m unittest discover -s tests -v`.

Bug reports and documentation feedback are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). For security issues, see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE) © 2026 Andre Romero
