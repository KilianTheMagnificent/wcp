"""Per-attempt wrapper (addendum B3/B3a): process-group leader spawned by `wcp run`.

    python3 -I /abs/path/to/wcp/_attempt.py <run_dir> <key> <attempt> <lock_fd> -- argv...

Holds the per-attempt flock the runner took before Popen (liveness = this lock, not a PID), writes
attempt-<key>-<n>.started.json, drains the builder's combined output through a bounded redacting
writer into builder-<key>-<n>.log, records markers, then writes attempt-<key>-<n>.exit.json and
exits with the builder's code. Standard library only; imports nothing else from wcp. The runner runs
it by absolute path in isolated mode so that a `wcp/` package inside the builder's worktree (this
repository, for one) can never shadow it.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import deque

HEAD_BYTES = 512 * 1024
TAIL_BYTES = 512 * 1024 - 256  # head + drop marker + tail stays under 1 MiB
MARKERS = (b'SCOPE-NEEDED:', b'KEEP-CANONICAL:')
MAX_MARKERS = 64            # distinct marker entries kept per attempt; the rest are dropped
MAX_MARKER_VALUE = 4096     # bytes of a marker value kept (a path; anything longer is not one)
SECRET_PATTERNS = tuple(re.compile(p) for p in (
    rb'sk-[A-Za-z0-9_-]{20,}',
    rb'gh[pousr]_[A-Za-z0-9]{20,}',
    rb'AKIA[0-9A-Z]{16}',
    rb'xox[abp]-[A-Za-z0-9-]+',
    rb'-----BEGIN[^-]*PRIVATE KEY-----',
    rb'(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*\S{8,}',
))


def write_private(path: str, data: bytes) -> None:
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, path)


def redact(line: bytes) -> bytes:
    for pat in SECRET_PATTERNS:
        if pat.search(line):
            return b'<redacted: %d chars>\n' % len(line.rstrip(b'\r\n'))
    return line


def main(argv: list) -> int:
    run_dir, key, attempt, lock_fd = argv[0], argv[1], argv[2], int(argv[3])
    if argv[4] != '--':
        raise SystemExit('usage: wcp._attempt RUN_DIR KEY ATTEMPT LOCK_FD -- ARGV...')
    cmd = argv[5:]
    os.set_inheritable(lock_fd, False)  # the lock lives exactly as long as this process; the builder never sees it
    signal.signal(signal.SIGTERM, lambda *_: None)  # killpg reaches the builder directly; we stay to record its exit
    base = os.path.join(run_dir, 'attempt-%s-%s' % (key, attempt))
    log_path = os.path.join(run_dir, 'builder-%s-%s.log' % (key, attempt))
    write_private(base + '.started.json', json.dumps(
        {'pid': os.getpid(), 'pgid': os.getpgid(0), 'started_at': time.time()}).encode())
    env = dict(os.environ)  # exactly what the runner built (B1); -I keeps this process's own sys.path clean
    markers: list = []
    tail: deque = deque()
    written = tail_bytes = dropped = 0
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(log_fd, 'wb', buffering=0) as log:
        try:
            p = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT)
        except OSError as e:
            log.write(('wcp: cannot start builder: %s\n' % e).encode())
            code = 127
        else:
            # ponytail: a grandchild that keeps the pipe open past the builder's exit keeps us draining;
            # the runner's timeout kills the whole group in that case
            with p.stdout:
                while True:
                    line = p.stdout.readline(65536)
                    if not line:
                        break
                    line = redact(line)  # before marker extraction: a secret-shaped marker line is not a marker
                    for m in MARKERS:
                        if m in line and len(markers) < MAX_MARKERS:
                            entry = {'marker': m[:-1].decode(),
                                     'value': line.split(m, 1)[1].strip()[:MAX_MARKER_VALUE].decode('utf-8', 'replace')}
                            if entry not in markers:
                                markers.append(entry)
                                write_private(base + '.markers.json', json.dumps(markers).encode())
                    if written + len(line) <= HEAD_BYTES:
                        log.write(line)
                        written += len(line)
                    else:
                        tail.append(line)
                        tail_bytes += len(line)
                        while tail_bytes > TAIL_BYTES:
                            old = tail.popleft()
                            tail_bytes -= len(old)
                            dropped += len(old)
            code = p.wait()
        if dropped:
            log.write(b'\n[wcp: %d bytes of output dropped here; first %d KiB and last %d KiB kept]\n'
                      % (dropped, HEAD_BYTES // 1024, TAIL_BYTES // 1024))
        for line in tail:
            log.write(line)
    write_private(base + '.exit.json', json.dumps({'exit_code': code, 'exited_at': time.time()}).encode())
    return code if code >= 0 else 128 - code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
