"""Path and secret policy: lexical normalization, name-only sensitive classifier,
descriptor-relative (dir_fd + O_NOFOLLOW) tree writes."""
from __future__ import annotations

import errno
import fnmatch
import hashlib
import os
import secrets
import stat
from pathlib import Path

from .db import ProtocolError

_REQUIRED_DIR_FD = {os.open, os.mkdir, os.unlink, os.rename, os.stat}
if not _REQUIRED_DIR_FD <= os.supports_dir_fd:
    raise RuntimeError("wcp needs dir_fd support for open, mkdir, unlink, rename, stat")
if os.chmod not in os.supports_dir_fd or os.chmod not in os.supports_follow_symlinks:
    raise RuntimeError("wcp needs os.chmod(dir_fd=, follow_symlinks=False)")

_SENSITIVE_DIRS = frozenset({".ssh", ".aws", ".kube", ".gnupg", ".docker"})
_SENSITIVE_EXEMPT = frozenset({".env.example", ".env.sample", ".env.template"})
_SENSITIVE_GLOBS = (
    ".env", ".env.*", "*.env", ".netrc", ".npmrc", ".pypirc", "*.pem", "*.key", "*.p12", "*.pfx",
    "*.keystore", "*.secret", "id_rsa*", "id_ed25519*", "id_ecdsa*", "credentials*.json",
    "credentials*.yaml", "credentials*.yml", "credentials*.ini", "service-account*.json", "secrets*",
)
_NOFOLLOW_ERRNOS = (errno.ELOOP, errno.ENOTDIR, errno.EMLINK)  # macOS says ENOTDIR, Linux ELOOP


def normalize_repo_path(p: str) -> str:
    p = os.fspath(p)
    if not isinstance(p, str) or p == "" or "\0" in p or p.startswith("/"):
        raise ProtocolError("unsafe_path", "unsafe path %r" % (p,), {"path": p})
    comps = p.split("/")
    for c in comps:
        if c in ("", ".", "..") or c.casefold() == ".git":
            raise ProtocolError("unsafe_path", "unsafe path %r" % (p,), {"path": p})
    return "/".join(comps)


def is_sensitive(p: str) -> bool:
    comps = [c.casefold() for c in os.fspath(p).split("/") if c]
    if not comps:
        return False
    if any(c in _SENSITIVE_DIRS for c in comps):
        return True
    base = comps[-1]
    if base in _SENSITIVE_EXEMPT:
        return False
    return any(fnmatch.fnmatchcase(base, g) for g in _SENSITIVE_GLOBS)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def git_mode(st_mode: int) -> str:
    return "100755" if st_mode & 0o111 else "100644"


def _unsafe(rel: str, component: str, why: str) -> ProtocolError:
    return ProtocolError("unsafe_path", "%s: %s (%s)" % (rel, why, component), {"path": rel, "component": component})


def open_dir_chain(tree: Path, rel_dir: str, create: bool) -> int:
    """Walk from `tree` one component at a time through held directory descriptors.

    The tree root is caller-trusted (opened by path, links allowed); every component below it is
    opened O_DIRECTORY|O_NOFOLLOW relative to its parent. Returns the final dir fd (caller closes).
    """
    fd = os.open(os.fspath(tree), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in [c for c in rel_dir.split("/") if c]:
            try:
                nfd = _open_dir(name, fd, rel_dir)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(name, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
                nfd = _open_dir(name, fd, rel_dir)
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_dir(name: str, parent_fd: int, rel_dir: str) -> int:
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError as e:
        if e.errno in _NOFOLLOW_ERRNOS:
            raise _unsafe(rel_dir, name, "not a real directory")
        raise


def _split(rel: str) -> tuple[str, str]:
    rel = normalize_repo_path(rel)
    head, _, name = rel.rpartition("/")
    return head, name


def _install_perm(name: str, fd: int, mode: str) -> int:
    """Permission bits for the replacement file: a new file takes the default for its git mode; an
    existing regular file keeps its own bits, with only the executable bits following the mode."""
    default = 0o755 if mode == "100755" else 0o644
    try:
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except OSError:
        return default
    if not stat.S_ISREG(st.st_mode):
        return default
    perm = st.st_mode & 0o7777
    return perm | ((perm & 0o444) >> 2) if mode == "100755" else perm & ~0o111


def atomic_write(tree: Path, rel: str, data: bytes, mode: str = "100644") -> None:
    rel_dir, name = _split(rel)
    fd = open_dir_chain(tree, rel_dir, create=True)
    try:
        perm = _install_perm(name, fd, mode)
        tmp = ".wcp-tmp-%s-%s" % (secrets.token_hex(4), name[:64])
        tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, perm, dir_fd=fd)
        try:
            with os.fdopen(tfd, "wb") as f:
                os.fchmod(f.fileno(), perm)
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=fd)
            except OSError:
                pass
            raise
    finally:
        os.close(fd)


def _stat_regular(name: str, fd: int, rel: str) -> os.stat_result:
    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode):
        raise _unsafe(rel, name, "not a regular file")
    return st


def set_exec_bit(tree: Path, rel: str, mode: str) -> None:
    """Set or clear the executable bits only; other permission bits are left alone."""
    rel_dir, name = _split(rel)
    fd = open_dir_chain(tree, rel_dir, create=False)
    try:
        perm = _stat_regular(name, fd, rel).st_mode & 0o7777
        if mode == "100755":
            new = perm | ((perm & 0o444) >> 2)
        else:
            new = perm & ~0o111
        if new != perm:
            os.chmod(name, new, dir_fd=fd, follow_symlinks=False)
    finally:
        os.close(fd)


def safe_unlink(tree: Path, rel: str) -> None:
    rel_dir, name = _split(rel)
    fd = open_dir_chain(tree, rel_dir, create=False)
    try:
        _stat_regular(name, fd, rel)
        os.unlink(name, dir_fd=fd)
    finally:
        os.close(fd)


def read_regular(tree: Path, rel: str) -> bytes:
    rel_dir, name = _split(rel)
    fd = open_dir_chain(tree, rel_dir, create=False)
    try:
        try:
            rfd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        except OSError as e:
            if e.errno in _NOFOLLOW_ERRNOS:
                raise _unsafe(rel, name, "not a regular file")
            raise
        with os.fdopen(rfd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise _unsafe(rel, name, "not a regular file")
            return f.read()
    finally:
        os.close(fd)
