from __future__ import annotations

import hashlib
import os
import shutil
import unittest
from pathlib import Path

from fixtures import commit, make_home, make_repo, write
from wcp import bundle, gitx
from wcp.db import ProtocolError

BIN = bytes(range(256))


def sha(b):
    return hashlib.sha256(b).hexdigest()


def exec_bit(p):
    return bool(os.stat(p).st_mode & 0o111)


class BundleBase(unittest.TestCase):
    def setUp(self):
        self.home = make_home()
        self.addCleanup(shutil.rmtree, str(self.home), True)

    def tree(self):
        """A repo with an extra committed 0755 script, so a modify of an executable is covered."""
        t = make_repo()
        self.addCleanup(shutil.rmtree, str(t), True)
        write(t, 'bin/run.sh', '#!/bin/sh\necho 1\n')
        os.chmod(t / 'bin/run.sh', 0o755)
        commit(t, 'script')
        return t

    def mutate(self, t):
        write(t, 'src/a.py', 'VALUE = 6\n')                 # modify
        write(t, 'bin/run.sh', '#!/bin/sh\necho 2\n')        # modify, keeps 100755
        write(t, 'new/subdir/file.py', 'NEW = 1\n')          # create, nested
        write(t, 'blob.bin', BIN)                            # create, binary
        os.unlink(t / 'src/b.py')                            # delete
        os.chmod(t / 'tests/check.py', 0o755)                # chmod only

    def bundle_from(self, t):
        baseline = gitx.snapshot(t)
        self.mutate(t)
        changes = bundle.diff_against_baseline(t, baseline)
        bdir = self.home / 'repos' / 'r' / 'bundles' / 't-000001' / 's-000001'
        manifest = bundle.write_bundle(bdir, t, changes)
        return baseline, changes, bdir, manifest


class RoundTripTests(BundleBase):
    def test_diff_write_apply(self):
        t = self.tree()
        baseline, changes, bdir, manifest = self.bundle_from(t)
        ops = {c.path: c.op for c in changes}
        self.assertEqual(ops, {'src/a.py': 'modify', 'bin/run.sh': 'modify', 'new/subdir/file.py': 'create',
                               'blob.bin': 'create', 'src/b.py': 'delete', 'tests/check.py': 'chmod'})
        by = {c.path: c for c in changes}
        chmod = by['tests/check.py']
        self.assertEqual(chmod.before, chmod.after)
        self.assertEqual((chmod.before_mode, chmod.after_mode), ('100644', '100755'))
        self.assertEqual(by['bin/run.sh'].after_mode, '100755')
        self.assertEqual(by['blob.bin'].after, sha(BIN))
        self.assertEqual(by['src/b.py'].before, sha(b'VALUE = 5\n'))
        self.assertIsNone(by['src/b.py'].after)
        self.assertIsNone(by['blob.bin'].before)
        # bundle on disk: blobs for every after hash, private modes, manifest round-trips
        for c in changes:
            if c.after:
                blob = bdir / 'blobs' / c.after
                self.assertEqual(sha(blob.read_bytes()), c.after)
                self.assertEqual(os.stat(blob).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(bdir).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(bdir / 'blobs').st_mode & 0o777, 0o700)
        self.assertEqual(bundle.read_manifest(bdir), manifest)
        self.assertEqual([d['path'] for d in manifest['changes']], [c.path for c in changes])
        self.assertEqual(manifest['changes'][0].keys(),
                         {'path', 'op', 'before_sha256', 'before_mode', 'after_sha256', 'after_mode'})
        self.assertEqual(bundle.Change.from_dict(manifest['changes'][0]), changes[0])
        # apply to a second identical tree
        other = self.tree()
        self.assertEqual(bundle.check_before_hashes(other, manifest), [])
        self.assertEqual(set(bundle.classify_publish(other, manifest).values()), {'before'})
        touched = bundle.apply_bundle(bdir, manifest, other)
        self.assertEqual(touched, [c.path for c in changes])
        self.assertEqual(gitx.snapshot(other), gitx.snapshot(t))
        self.assertEqual((other / 'blob.bin').read_bytes(), BIN)
        self.assertTrue((other / 'new' / 'subdir').is_dir())
        self.assertEqual((other / 'new/subdir/file.py').read_text(), 'NEW = 1\n')
        self.assertFalse((other / 'src/b.py').exists())
        self.assertTrue(exec_bit(other / 'tests/check.py'))
        self.assertTrue(exec_bit(other / 'bin/run.sh'))
        self.assertFalse(exec_bit(other / 'src/a.py'))
        self.assertEqual(set(bundle.classify_publish(other, manifest).values()), {'after'})
        self.assertEqual(bundle.check_before_hashes(other, manifest), [c.path for c in changes])
        # no change -> empty diff; a bundle of nothing is still a valid manifest
        self.assertEqual(bundle.diff_against_baseline(t, gitx.snapshot(t)), [])

    def test_only_and_blob_integrity(self):
        t = self.tree()
        _, changes, bdir, manifest = self.bundle_from(t)
        other = self.tree()
        self.assertEqual(bundle.apply_bundle(bdir, manifest, other, only=['src/a.py']), ['src/a.py'])
        self.assertEqual((other / 'src/a.py').read_text(), 'VALUE = 6\n')
        self.assertTrue((other / 'src/b.py').exists())
        blob = bdir / 'blobs' / sha(BIN)
        blob.write_bytes(b'corrupt')
        with self.assertRaises(ProtocolError) as cm:
            bundle.apply_bundle(bdir, manifest, other, only=['blob.bin'])
        self.assertEqual(cm.exception.code, 'bad_state')
        self.assertFalse((other / 'blob.bin').exists())

    def test_write_bundle_refuses_when_tree_moved(self):
        t = self.tree()
        baseline = gitx.snapshot(t)
        write(t, 'src/a.py', 'VALUE = 6\n')
        changes = bundle.diff_against_baseline(t, baseline)
        write(t, 'src/a.py', 'VALUE = 7\n')
        with self.assertRaises(ProtocolError) as cm:
            bundle.write_bundle(self.home / 'b', t, changes)
        self.assertEqual(cm.exception.code, 'bad_state')


class UnsupportedTests(BundleBase):
    def test_symlink_changes_refuse(self):
        t = self.tree()
        os.symlink('README.md', t / 'link')
        baseline = gitx.snapshot(t)
        self.assertEqual(baseline['link'][1], 'symlink')
        self.assertEqual(bundle.diff_against_baseline(t, baseline), [])
        os.unlink(t / 'link')
        os.symlink('src/a.py', t / 'link')
        with self.assertRaises(ProtocolError) as cm:
            bundle.diff_against_baseline(t, baseline)
        self.assertEqual(cm.exception.code, 'unsupported_path')
        self.assertEqual(cm.exception.detail['paths'], ['link'])
        os.unlink(t / 'link')
        with self.assertRaises(ProtocolError) as cm:            # deleted symlink
            bundle.diff_against_baseline(t, baseline)
        self.assertEqual(cm.exception.code, 'unsupported_path')
        write(t, 'link', 'now a file\n')
        with self.assertRaises(ProtocolError):                  # symlink replaced by a file
            bundle.diff_against_baseline(t, baseline)
        os.unlink(t / 'link')
        os.symlink('README.md', t / 'link')
        os.symlink('README.md', t / 'newlink')
        with self.assertRaises(ProtocolError) as cm:            # created symlink
            bundle.diff_against_baseline(t, baseline)
        self.assertEqual(cm.exception.detail['paths'], ['newlink'])
        os.unlink(t / 'newlink')
        os.unlink(t / 'src/a.py')
        os.symlink('b.py', t / 'src/a.py')                       # file replaced by a symlink
        with self.assertRaises(ProtocolError) as cm:
            bundle.diff_against_baseline(t, baseline)
        self.assertEqual(cm.exception.detail['paths'], ['src/a.py'])


class HashGateTests(BundleBase):
    def test_check_before_hashes_drift_per_op(self):
        t = self.tree()
        _, changes, bdir, manifest = self.bundle_from(t)
        other = self.tree()
        self.assertEqual(bundle.check_before_hashes(other, manifest), [])
        write(other, 'src/a.py', 'VALUE = 9\n')                          # modify: content drifted
        self.assertEqual(bundle.check_before_hashes(other, manifest), ['src/a.py'])
        write(other, 'src/a.py', 'VALUE = 5\n')
        write(other, 'new/subdir/file.py', 'NEW = 1\n')                  # create: already present (even if equal)
        self.assertEqual(bundle.check_before_hashes(other, manifest), ['new/subdir/file.py'])
        shutil.rmtree(other / 'new')
        os.unlink(other / 'src/b.py')                                    # delete: already gone
        self.assertEqual(bundle.check_before_hashes(other, manifest), ['src/b.py'])
        write(other, 'src/b.py', 'VALUE = 5\n')
        os.chmod(other / 'tests/check.py', 0o755)                        # chmod: mode drifted, hash equal
        self.assertEqual(bundle.check_before_hashes(other, manifest), ['tests/check.py'])
        os.chmod(other / 'tests/check.py', 0o644)
        os.unlink(other / 'bin/run.sh')
        os.symlink('../README.md', other / 'bin/run.sh')                 # modify: not a regular file
        self.assertEqual(bundle.check_before_hashes(other, manifest), ['bin/run.sh'])
        os.unlink(other / 'bin/run.sh')
        os.mkdir(other / 'blob.bin')                                     # create: a directory is in the way
        self.assertEqual(sorted(bundle.check_before_hashes(other, manifest)), ['bin/run.sh', 'blob.bin'])

    def test_classify_publish(self):
        t = self.tree()
        _, changes, bdir, manifest = self.bundle_from(t)
        other = self.tree()
        bundle.apply_bundle(bdir, manifest, other, only=['src/a.py', 'src/b.py', 'tests/check.py'])
        cls = bundle.classify_publish(other, manifest)
        self.assertEqual(cls['src/a.py'], 'after')
        self.assertEqual(cls['src/b.py'], 'after')          # deleted
        self.assertEqual(cls['tests/check.py'], 'after')    # bit set
        self.assertEqual(cls['bin/run.sh'], 'before')
        self.assertEqual(cls['blob.bin'], 'before')         # create, still absent
        self.assertEqual(cls['new/subdir/file.py'], 'before')
        write(other, 'bin/run.sh', '#!/bin/sh\necho 3\n')
        write(other, 'blob.bin', b'something else')
        write(other, 'src/b.py', 'VALUE = 5\n')
        os.chmod(other / 'src/b.py', 0o755)
        os.chmod(other / 'src/a.py', 0o755)
        cls = bundle.classify_publish(other, manifest)
        self.assertEqual(cls['bin/run.sh'], 'other')
        self.assertEqual(cls['blob.bin'], 'other')
        self.assertEqual(cls['src/b.py'], 'other')          # right bytes, wrong mode
        self.assertEqual(cls['src/a.py'], 'other')          # after bytes, mode drifted
        # finishing the remaining 'before' paths brings everything to 'after'
        os.chmod(other / 'src/a.py', 0o644)
        os.unlink(other / 'src/b.py')
        remaining = [p for p, s in bundle.classify_publish(other, manifest).items() if s == 'before']
        self.assertEqual(remaining, ['new/subdir/file.py'])
        bundle.apply_bundle(bdir, manifest, other, only=remaining)
        self.assertEqual(bundle.classify_publish(other, manifest)['new/subdir/file.py'], 'after')


class ApplySafetyTests(BundleBase):
    def test_apply_refuses_symlinked_parent(self):
        t = self.tree()
        _, changes, bdir, manifest = self.bundle_from(t)
        other = self.tree()
        elsewhere = Path(self.home / 'elsewhere')
        elsewhere.mkdir()
        os.symlink(str(elsewhere), other / 'new')
        with self.assertRaises(ProtocolError) as cm:
            bundle.apply_bundle(bdir, manifest, other, only=['new/subdir/file.py'])
        self.assertEqual(cm.exception.code, 'unsafe_path')
        self.assertEqual(os.listdir(elsewhere), [])
        os.unlink(other / 'new')
        # a symlinked leaf is drift for the hash gate; even a forced apply replaces the link
        # itself and never writes through it
        os.unlink(other / 'src/a.py')
        os.symlink(str(elsewhere / 'victim'), other / 'src/a.py')
        self.assertIn('src/a.py', bundle.check_before_hashes(other, manifest))
        self.assertEqual(bundle.classify_publish(other, manifest)['src/a.py'], 'other')
        bundle.apply_bundle(bdir, manifest, other, only=['src/a.py'])
        self.assertFalse((elsewhere / 'victim').exists())
        self.assertFalse(os.path.islink(other / 'src/a.py'))
        self.assertEqual((other / 'src/a.py').read_text(), 'VALUE = 6\n')
        # a symlinked leaf is never unlinked through
        os.unlink(other / 'src/b.py')
        os.symlink(str(elsewhere / 'victim'), other / 'src/b.py')
        with self.assertRaises(ProtocolError):
            bundle.apply_bundle(bdir, manifest, other, only=['src/b.py'])
        self.assertTrue(os.path.islink(other / 'src/b.py'))

    def test_manifest_paths_are_normalized(self):
        t = self.tree()
        _, changes, bdir, manifest = self.bundle_from(t)
        bad = {'changes': [dict(manifest['changes'][0], path='../escape.py')], 'created_at': 0}
        other = self.tree()
        with self.assertRaises(ProtocolError) as cm:
            bundle.apply_bundle(bdir, bad, other)
        self.assertEqual(cm.exception.code, 'unsafe_path')
        with self.assertRaises(ProtocolError):
            bundle.diff_against_baseline(other, {'../x': ('0' * 64, '100644')})


if __name__ == '__main__':
    unittest.main()
