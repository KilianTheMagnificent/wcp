from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from wcp import paths
from wcp.db import ProtocolError

SENSITIVE = [
    ".env", ".env.local", "config/.env.production", "prod.env", "experiment.env", ".ENV",
    ".netrc", ".npmrc", ".pypirc", "certs/server.pem", "k.key", "x.p12", "x.pfx", "app.keystore",
    "deploy.secret", "id_rsa", "id_rsa.pub", "id_ed25519", "id_ecdsa.pub", "credentials.json",
    "credentials-prod.yaml", "credentials.yml", "credentials.ini", "service-account-123.json",
    "secrets.yaml", "secrets", ".ssh/config", "home/.aws/credentials", ".kube/config",
    ".gnupg/pubring.kbx", ".docker/config.json", "a/.SSH/id_x",
]
NOT_SENSITIVE = [
    ".env.example", ".env.sample", ".env.template", "sub/.env.example", "environment.md",
    "src/keys.py", "tokenizer.py", "README.md", "src/a.py", "package.json", "config.yaml",
    "docker-compose.yml", "monkey.py", "keyboard.ts", "envelope.py", "secret_sauce.md",
    ".github/CODEOWNERS", ".github/dependabot.yml", ".github/ISSUE_TEMPLATE/bug.md",
    "src/workflows/order.py", "mcp.json", "claude.py",
    # Commonly tracked repo files: classing them as sensitive would refuse provisioning for every
    # repo that tracks one (gitx.provision_worktree refuses tracked sensitive paths).
    ".gitattributes", ".gitmodules", ".github/workflows/ci.yml", ".agent-tools.json", ".mcp.json",
    ".claude/settings.json",
]


class NormalizeTests(unittest.TestCase):
    def test_accepts(self):
        for p in ["src/a.py", "a/b/c", "README.md", "src/*.py", "src/**", ".gitignore", "a.git/x"]:
            with self.subTest(p=p):
                self.assertEqual(paths.normalize_repo_path(p), p)

    def test_rejects(self):
        for p in ["/abs/x", "../x", "a/../b", ".", "a/./b", "a\0b", ".git", ".git/x", "a/.git/b",
                  ".GIT/config", "", "a/", "a//b", "..", "/"]:
            with self.subTest(p=repr(p)):
                with self.assertRaises(ProtocolError) as cm:
                    paths.normalize_repo_path(p)
                self.assertEqual(cm.exception.code, "unsafe_path")


class SensitiveTests(unittest.TestCase):
    def test_positives(self):
        self.assertGreaterEqual(len(SENSITIVE), 15)
        for p in SENSITIVE:
            with self.subTest(p=p):
                self.assertTrue(paths.is_sensitive(p))

    def test_negatives(self):
        self.assertGreaterEqual(len(NOT_SENSITIVE), 8)
        for p in NOT_SENSITIVE:
            with self.subTest(p=p):
                self.assertFalse(paths.is_sensitive(p))

    def test_gitignore_covers_secret_and_local_patterns(self):
        gitignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(encoding="utf-8")
        lines = {line.strip() for line in gitignore.splitlines() if line.strip()}
        for wanted in (".env", ".env.*", "*.pem", "*.key", ".DS_Store", ".venv*/"):
            self.assertIn(wanted, lines)


class TreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wcp-paths-"))
        self.tree = self.tmp / "tree"
        self.tree.mkdir()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _mode(self, rel):
        return stat.S_IMODE(os.lstat(self.tree / rel).st_mode)

    def test_open_dir_chain_creates_nested(self):
        fd = paths.open_dir_chain(self.tree, "a/b/c", create=True)
        try:
            self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
            self.assertEqual(os.fstat(fd).st_ino, os.stat(self.tree / "a/b/c").st_ino)
        finally:
            os.close(fd)
        self.assertTrue((self.tree / "a/b/c").is_dir())
        with self.assertRaises(FileNotFoundError):
            paths.open_dir_chain(self.tree, "a/missing", create=False)
        fd = paths.open_dir_chain(self.tree, "", create=False)
        os.close(fd)

    def test_open_dir_chain_refuses_symlink_component(self):
        os.symlink(self.outside, self.tree / "link")
        for create in (False, True):
            with self.assertRaises(ProtocolError) as cm:
                paths.open_dir_chain(self.tree, "link/sub", create=create)
            self.assertEqual(cm.exception.code, "unsafe_path")
        self.assertEqual(os.listdir(self.outside), [])
        (self.tree / "file").write_bytes(b"x")
        with self.assertRaises(ProtocolError):
            paths.open_dir_chain(self.tree, "file/sub", create=True)

    def test_atomic_write_nested_and_modes(self):
        paths.atomic_write(self.tree, "new/subdir/file.py", b"print(1)\n")
        self.assertEqual((self.tree / "new/subdir/file.py").read_bytes(), b"print(1)\n")
        self.assertEqual(self._mode("new/subdir/file.py"), 0o644)
        paths.atomic_write(self.tree, "new/run.sh", b"#!/bin/sh\n", mode="100755")
        self.assertEqual(self._mode("new/run.sh"), 0o755)
        paths.atomic_write(self.tree, "new/subdir/file.py", b"print(2)\n", mode="100755")
        self.assertEqual((self.tree / "new/subdir/file.py").read_bytes(), b"print(2)\n")
        self.assertEqual(self._mode("new/subdir/file.py"), 0o755)
        self.assertEqual(sorted(os.listdir(self.tree / "new/subdir")), ["file.py"])  # no temp left behind

    def test_atomic_write_refuses_symlinked_parent_and_unsafe(self):
        os.symlink(self.outside, self.tree / "link")
        with self.assertRaises(ProtocolError) as cm:
            paths.atomic_write(self.tree, "link/x.py", b"x")
        self.assertEqual(cm.exception.code, "unsafe_path")
        self.assertEqual(os.listdir(self.outside), [])
        with self.assertRaises(ProtocolError):
            paths.atomic_write(self.tree, "../escape.py", b"x")
        self.assertFalse((self.tmp / "escape.py").exists())

    def test_set_exec_bit_flips_only_exec(self):
        f = self.tree / "s.sh"
        f.write_bytes(b"x")
        os.chmod(f, 0o644)
        paths.set_exec_bit(self.tree, "s.sh", "100755")
        self.assertEqual(self._mode("s.sh"), 0o755)
        paths.set_exec_bit(self.tree, "s.sh", "100644")
        self.assertEqual(self._mode("s.sh"), 0o644)
        os.chmod(f, 0o600)
        paths.set_exec_bit(self.tree, "s.sh", "100755")
        self.assertEqual(self._mode("s.sh"), 0o700)
        self.assertEqual(paths.git_mode(os.stat(f).st_mode), "100755")
        paths.set_exec_bit(self.tree, "s.sh", "100644")
        self.assertEqual(self._mode("s.sh"), 0o600)
        self.assertEqual(paths.git_mode(os.stat(f).st_mode), "100644")

    def test_set_exec_bit_refuses_symlink(self):
        target = self.outside / "t"
        target.write_bytes(b"x")
        os.chmod(target, 0o644)
        os.symlink(target, self.tree / "l")
        with self.assertRaises(ProtocolError) as cm:
            paths.set_exec_bit(self.tree, "l", "100755")
        self.assertEqual(cm.exception.code, "unsafe_path")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o644)

    def test_safe_unlink(self):
        (self.tree / "gone.txt").write_bytes(b"x")
        paths.safe_unlink(self.tree, "gone.txt")
        self.assertFalse((self.tree / "gone.txt").exists())
        target = self.outside / "t"
        target.write_bytes(b"x")
        os.symlink(target, self.tree / "l")
        with self.assertRaises(ProtocolError) as cm:
            paths.safe_unlink(self.tree, "l")
        self.assertEqual(cm.exception.code, "unsafe_path")
        self.assertTrue(os.path.islink(self.tree / "l"))
        self.assertTrue(target.exists())
        (self.tree / "d").mkdir()
        with self.assertRaises(ProtocolError):
            paths.safe_unlink(self.tree, "d")
        self.assertTrue((self.tree / "d").is_dir())

    def test_read_regular(self):
        (self.tree / "a.bin").write_bytes(b"\x00\xff")
        self.assertEqual(paths.read_regular(self.tree, "a.bin"), b"\x00\xff")
        os.symlink(self.tree / "a.bin", self.tree / "l")
        with self.assertRaises(ProtocolError) as cm:
            paths.read_regular(self.tree, "l")
        self.assertEqual(cm.exception.code, "unsafe_path")

    def test_sha256(self):
        self.assertEqual(paths.sha256_bytes(b""),
                         "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")


if __name__ == "__main__":
    unittest.main()
