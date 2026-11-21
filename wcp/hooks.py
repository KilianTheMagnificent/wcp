"""PreToolUse hook: one handler for Claude Code, Codex, Grok and Qwen.

Deny = exit 2 with a one-line reason on stderr; allow = exit 0 with empty stdout; never JSON on
stdout and never tool content in output or logs. Read-only SQLite plus realpath prefix tests, no
git. Fails open on any internal error (one line appended to WCP_HOME/hook-errors.log).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import time
import urllib.parse
from pathlib import Path

from . import db, scope
from .db import ProtocolError
from .model import ACTIVE_STATES
from .paths import is_sensitive, normalize_repo_path

WRITABLE_STATES = frozenset({"claimed", "running", "blocked", "conflicted"})
_PATH_KEYS = ("file_path", "notebook_path", "path", "filePath")
_PATCH_FIELDS = ("patch", "input")  # Codex apply_patch carries the patch here (tool_input is schema-free)
_CONTENT_FIELDS = ("content", "new_string", "old_string", "new_source")  # file bodies: never scanned
_PATCH_LINE = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+?)\s*$", re.M)
_DIALECTS = {  # matcher, timeout (Claude/Codex/Grok seconds; Qwen milliseconds)
    "claude": ("Edit|Write|NotebookEdit|MultiEdit", 10),
    "codex": ("apply_patch|Edit|Write", 30),
    # Grok 1.0.5: native tool ids first (hashline_edit is the alternate file bundle), then the Claude
    # names it aliases; its own default hook budget is 5 s, so the timeout is stated.
    "grok": ("write|search_replace|edit_notebook|delete_file|hashline_edit|Edit|Write|MultiEdit|NotebookEdit", 10),
    "qwen": ("write_file|edit|WriteFile", 10000),
}


def extract_paths(tool_input: dict, tool_name: str = "") -> list[str]:
    """Candidate paths: the known path keys, plus apply_patch file lines from the patch itself.

    Patch lines are read only from an `apply_patch` call or from a field named patch/input, and
    never from a file body, so a Write whose content documents a patch is not mistaken for one.
    """
    out: list[str] = []
    for key in _PATH_KEYS:
        v = tool_input.get(key)
        if isinstance(v, str) and v:
            out.append(v)
    patch_tool = str(tool_name or "").casefold() == "apply_patch"
    for key, v in tool_input.items():
        if not isinstance(v, str) or key in _CONTENT_FIELDS:
            continue
        if patch_tool or key in _PATCH_FIELDS:
            out.extend(_PATCH_LINE.findall(v))
    return list(dict.fromkeys(out))


def normalize(payload: dict) -> dict:
    """{'tool_name','tool_input','cwd'} from snake_case (Claude/Codex/Qwen) or camelCase (Grok)."""
    tool_input = payload.get("tool_input", payload.get("toolInput"))
    if isinstance(tool_input, str):  # Codex tool_input is schema-free; a bare patch string
        tool_input = {"patch": tool_input}
    elif not isinstance(tool_input, dict):
        tool_input = {}
    return {
        "tool_name": payload.get("tool_name") or payload.get("toolName") or "",
        "tool_input": tool_input,
        "cwd": payload.get("cwd") or payload.get("workspaceRoot") or os.getcwd(),
    }


def _home(home, env) -> Path:
    if home:
        return Path(home)
    h = (os.environ if env is None else env).get("WCP_HOME")
    return Path(h) if h else db.DEFAULT_HOME()


def _candidate_repo_ids(home: Path, target_paths: list[str], env) -> set[str]:
    """Candidate repo_ids that could contain any target path (either as a canonical ancestor or under WCP_HOME/repos/<id>)."""
    rids: set[str] = set()
    repos_dir = os.path.realpath(home / "repos")
    scan = list(target_paths)
    env_wt = env.get("WCP_WORKTREE") if env else None
    if env_wt:
        scan.append(os.path.realpath(env_wt))
    prefix = repos_dir.rstrip("/") + "/"
    for p in scan:
        if p.startswith(prefix):
            rid = p[len(prefix):].split("/", 1)[0]
            if rid:
                rids.add(rid)
        cur = Path(p)
        for d in (cur, *cur.parents):
            rids.add(hashlib.sha256(str(d).encode("utf-8")).hexdigest()[:16])
    return rids


def _read_registry_state(
    home: Path,
    target_paths: list[str] | None = None,
    env=None,
) -> tuple[list[dict], list[tuple[str, str, sqlite3.OperationalError]]]:
    """(repos, locked) from read-only opens of state DBs; OperationalError is captured per repo.

    When target_paths is given, unrelated repos and closed historical worktrees/tasks are skipped.
    """
    repos = []
    locked = []
    db_paths = sorted((home / "repos").glob("*/state.sqlite3"))
    if target_paths is not None:
        cand_rids = _candidate_repo_ids(home, target_paths, env)
        if cand_rids or not (env and env.get("WCP_ROLE") == "builder"):
            db_paths = [p for p in db_paths if p.parent.name in cand_rids]
    active_list = sorted(ACTIVE_STATES)
    q_states = ",".join("?" for _ in active_list)
    for db_path in db_paths:
        real_wt_root = os.path.realpath(db_path.parent / "worktrees")
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(str(db_path)), uri=True, timeout=2.0)
            conn.row_factory = sqlite3.Row
            try:
                root = db.meta_get(conn, "root")
                if not root:
                    continue
                real_root = os.path.realpath(root)
                if target_paths is None:
                    tasks = {r["id"]: dict(r) for r in conn.execute(
                        "SELECT id, state, epoch, lease_expires_at FROM tasks")}
                    scopes: dict[str, list[str]] = {}
                    for r in conn.execute("SELECT task_id, pattern FROM task_scopes WHERE kind='path' ORDER BY pattern"):
                        scopes.setdefault(r["task_id"], []).append(r["pattern"])
                    worktrees = [dict(r) for r in conn.execute("SELECT path, task_id, epoch, status FROM worktrees")]
                else:
                    wt_prefix = real_wt_root.rstrip("/") + "/"
                    wt_names = {
                        p[len(wt_prefix):].split("/", 1)[0]
                        for p in target_paths
                        if p.startswith(wt_prefix)
                    }
                    wt_names.discard("")
                    hits_root = any(_under(p, real_root) for p in target_paths)
                    if not hits_root and not wt_names:
                        continue
                    worktrees = [dict(r) for r in conn.execute(
                        "SELECT path, task_id, epoch, status FROM worktrees WHERE status != 'retired'")]
                    if wt_names:
                        clauses = []
                        params: list = []
                        for name in sorted(wt_names):
                            suffix = "/" + name
                            clauses.append("substr(path, -?) = ?")
                            params.extend([len(suffix), suffix])
                        worktrees.extend(dict(r) for r in conn.execute(
                            "SELECT path, task_id, epoch, status FROM worktrees "
                            "WHERE status = 'retired' AND (" + " OR ".join(clauses) + ")",
                            params,
                        ))
                    wt_tids = sorted({w["task_id"] for w in worktrees if w["task_id"]})
                    if wt_tids:
                        q_tids = ",".join("?" for _ in wt_tids)
                        task_sql = (
                            f"SELECT id, state, epoch, lease_expires_at FROM tasks "
                            f"WHERE state IN ({q_states}) OR id IN ({q_tids})"
                        )
                        task_params = active_list + wt_tids
                    else:
                        task_sql = f"SELECT id, state, epoch, lease_expires_at FROM tasks WHERE state IN ({q_states})"
                        task_params = active_list
                    tasks = {r["id"]: dict(r) for r in conn.execute(task_sql, task_params)}
                    scopes = {}
                    if wt_tids:
                        q_tids = ",".join("?" for _ in wt_tids)
                        for r in conn.execute(
                            f"SELECT task_id, pattern FROM task_scopes WHERE kind='path' "
                            f"AND task_id IN ({q_tids}) ORDER BY pattern",
                            wt_tids,
                        ):
                            scopes.setdefault(r["task_id"], []).append(r["pattern"])
            finally:
                conn.close()
        except sqlite3.OperationalError as e:
            locked.append((db_path.parent.name, real_wt_root, e))
            continue
        repos.append({"root": real_root, "tasks": tasks, "scopes": scopes, "worktrees": worktrees})
    return repos, locked


def _read_registry(home: Path) -> list[dict]:
    """One dict per registered repo, from a read-only open of its state DB (never creates one)."""
    repos, locked = _read_registry_state(home)
    if locked:
        raise locked[0][2]
    return repos


_DEFAULT_READ_REGISTRY = _read_registry


def _git_worktree_repo_id(path: str) -> str | None:
    cur = Path(path)
    for d in (cur, *cur.parents):
        dotgit = d / ".git"
        try:
            st = os.lstat(dotgit)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            return None
        try:
            first = dotgit.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        except (OSError, IndexError):
            return None
        if not first.startswith("gitdir:"):
            return None
        gitdir = Path(first[len("gitdir:"):].strip())
        gitdir = gitdir.resolve() if gitdir.is_absolute() else (d / gitdir).resolve()
        if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
            return db.repo_id(gitdir.parent.parent.parent)
        return None
    return None


def _in_locked_worktree(path: str, locked: list[tuple[str, str, sqlite3.OperationalError]], env) -> bool:
    if any(wt_root and _under(path, wt_root) for _, wt_root, _ in locked):
        return True
    env_wt = env.get("WCP_WORKTREE")
    if env_wt and _under(path, os.path.realpath(env_wt)):
        return True
    rid = _git_worktree_repo_id(path)
    return rid is not None and any(rid == locked_rid for locked_rid, _, _ in locked if locked_rid)


def _roots(repos: list[dict]) -> list[dict]:
    roots = []
    for repo in repos:
        roots.append({"path": repo["root"], "kind": "canonical", "repo": repo, "wt": None})
        for wt in repo["worktrees"]:
            roots.append({"path": os.path.realpath(wt["path"]), "kind": "worktree", "repo": repo, "wt": wt})
    roots.sort(key=lambda r: len(r["path"]), reverse=True)  # longest prefix wins
    return roots


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _containing(path: str, roots: list[dict]):
    for r in roots:
        if _under(path, r["path"]):
            return r
    return None


def _safe(rel: str) -> bool:
    try:
        normalize_repo_path(rel)
        return True
    except ProtocolError:
        return False


def decide(payload: dict, home: Path | None = None, env: dict | None = None) -> tuple[bool, str]:
    """(allow, reason). Read-only; no git. First deny wins, in the spec's order."""
    env = os.environ if env is None else env
    norm = normalize(payload)
    candidates = extract_paths(norm["tool_input"], norm["tool_name"])
    if not candidates:
        return True, "no path"
    home_dir = _home(home, env)
    cwd = os.path.realpath(norm["cwd"])
    target_paths = [cwd] + [
        os.path.realpath(c if os.path.isabs(c) else os.path.join(cwd, c))
        for c in candidates if "\0" not in c
    ]
    if _read_registry is _DEFAULT_READ_REGISTRY:
        repos, locked = _read_registry_state(home_dir, target_paths=target_paths, env=env)
    else:
        try:
            repos, locked = _read_registry(home_dir), []
        except sqlite3.OperationalError as e:
            repos = []
            locked = [(p.parent.name, os.path.realpath(p.parent / "worktrees"), e)
                      for p in sorted((home_dir / "repos").glob("*/state.sqlite3"))] or [("", "", e)]
    for _, _, exc in locked:
        _log_error(home, env, exc)
    roots = _roots(repos)
    if locked and (_in_locked_worktree(cwd, locked, env) or env.get("WCP_ROLE") == "builder"):
        return False, "wcp: state database unavailable; refusing write inside registered worktree"
    cwd_hit = _containing(cwd, roots)
    cwd_wt = cwd_hit if cwd_hit is not None and cwd_hit["kind"] == "worktree" else None
    at = db.now()
    for cand in candidates:
        reason = _decide_path(cand, cwd, cwd_wt, roots, locked, env, at)
        if reason is not None:
            return False, reason
    return True, "allowed"


def _decide_path(cand: str, cwd: str, cwd_wt, roots: list[dict], locked, env, at: float):
    if "\0" in cand:
        return "wcp: unsafe path"
    real = os.path.realpath(cand if os.path.isabs(cand) else os.path.join(cwd, cand))
    if locked and _in_locked_worktree(real, locked, env):
        return "wcp: state database unavailable; refusing write inside registered worktree"
    hit = _containing(real, roots)
    if hit is not None:
        rel = os.path.relpath(real, hit["path"])
        if ".." in cand.split("/") or not _safe(rel):
            return "wcp: unsafe path"
    if cwd_wt is not None and not _under(real, cwd_wt["path"]):
        return "wcp: a task builder may write only inside its worktree %s" % cwd_wt["path"]
    if hit is None:
        return None  # not a wcp-registered path: opt-in tool, no opinion
    if is_sensitive(rel):
        return "wcp: sensitive path %s cannot be modified" % rel
    if hit["kind"] == "worktree":
        return _worktree_rule(rel, hit, at)
    return _canonical_rule(hit["repo"], env)


def _worktree_rule(rel: str, hit: dict, at: float):
    wt, repo = hit["wt"], hit["repo"]
    tid = wt["task_id"]
    if wt["status"] == "retired":
        return ("wcp: worktree %s was retired at epoch %s; the task was reclaimed - run 'wcp task recover %s'"
                % (wt["path"], wt["epoch"], tid))
    if wt["status"] != "active":
        return "wcp: worktree %s is still provisioning; run 'wcp task recover %s'" % (wt["path"], tid)
    task = repo["tasks"].get(tid)
    if task is None or task["state"] not in WRITABLE_STATES:
        return "wcp: task %s is %s, not writable; run 'wcp task recover %s'" % (
            tid, task["state"] if task else "missing", tid)
    if task["lease_expires_at"] is None or at >= task["lease_expires_at"]:
        return "wcp: lease for %s expired; run 'wcp task recover %s'" % (tid, tid)
    patterns = repo["scopes"].get(tid, [])
    if not scope.matches(patterns, rel):
        return ("wcp: %s is outside task %s scope [%s]; request expansion with "
                "'wcp scope request %s --owner <you> --epoch %s --write <pattern> --reason <why>'"
                % (rel, tid, ", ".join(patterns), tid, task["epoch"]))
    return None


def _canonical_rule(repo: dict, env):
    if not any(t["state"] in ACTIVE_STATES for t in repo["tasks"].values()):
        return None
    if env.get("WCP_ROLE") == "integrator":
        return None
    active = ", ".join(w["path"] for w in repo["worktrees"] if w["status"] == "active")
    return ("wcp: canonical checkout is integrator-only while tasks are active; "
            "work in your task worktree (%s)" % (active or "none active yet"))


def _log_error(home, env, exc: BaseException) -> None:
    try:
        h = _home(home, env)
        db.ensure_private(h, True)
        log = h / "hook-errors.log"
        with open(log, "a", encoding="utf-8") as f:
            f.write("%s %s: %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), type(exc).__name__,
                                     str(exc).replace("\n", " ")[:300]))
        os.chmod(log, 0o600)
    except Exception:
        pass  # logging must never turn a fail-open into a crash


def main_pretooluse(stdin_text: str, home=None, env=None) -> int:
    """0 and no stdout on allow; one reason line on stderr and 2 on deny; any exception → allow."""
    try:
        payload = json.loads(stdin_text)
        if not isinstance(payload, dict):
            raise TypeError("hook payload is not a JSON object")
        allow, reason = decide(payload, home, env)
    except Exception as e:  # fail open: a broken hook must not lock a user out of their editor
        _log_error(home, env, e)
        return 0
    if allow:
        return 0
    sys.stderr.write(reason + "\n")
    sys.stderr.flush()
    return 2


def settings_snippet(dialect: str, wcp_cmd: str) -> dict:
    """Config block for 'claude' (also read natively by Grok), 'codex', 'grok', or 'qwen'.

    All four take the same shape - one top-level "hooks" object keyed by PascalCase event name,
    each matcher group holding a "hooks" list of command handlers - so one merge serves them all.
    Codex: ~/.codex/hooks.json uses that single top-level "hooks" key,
    i.e. the Claude shape. Grok: ~/.grok/hooks/*.json (global, always trusted) or
    <repo>/.grok/hooks/*.json (needs /hooks-trust); its own block is needed once a session no longer
    imports Claude config. Grok's timeout is seconds; Qwen command-hook timeouts are milliseconds.
    Every dialect blocks on exit 2 and takes the first stderr line as the reason.
    """
    if dialect not in _DIALECTS:
        raise ValueError("unknown hook dialect %r" % (dialect,))
    matcher, timeout = _DIALECTS[dialect]
    return {"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [
        {"type": "command", "command": "%s hook pretooluse" % wcp_cmd, "timeout": timeout}]}]}}


def install(dialect: str, into: Path, wcp_cmd: str) -> Path:
    """Merge a dialect's block into a hook file (created if absent); other keys untouched; idempotent.

    `into` is the file the caller names: a Claude/Qwen settings.json, ~/.codex/hooks.json, or a
    ~/.grok/hooks/<name>.json. Only the one PreToolUse entry is added, and only when no entry
    already runs the same command; every other key and hook entry in the file is left as it was.
    """
    into = Path(into)
    text = into.read_text(encoding="utf-8") if into.exists() else ""
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError("%s is not a JSON object" % into)
    block = settings_snippet(dialect, wcp_cmd)["hooks"]["PreToolUse"][0]
    command = block["hooks"][0]["command"]
    entries = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
    present = any(h.get("command") == command for e in entries if isinstance(e, dict)
                  for h in e.get("hooks", []) if isinstance(h, dict))
    if not present:
        entries.append(block)
    into.parent.mkdir(parents=True, exist_ok=True)
    into.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return into


def install_claude(into: Path, wcp_cmd: str) -> Path:
    """Back-compatible alias of install('claude', ...)."""
    return install("claude", into, wcp_cmd)
