from __future__ import annotations

import unicodedata
import unittest

from wcp import scope

MATCH_TABLE = [
    # pattern, path, expected
    ("src/**", "src/a.py", True),
    ("src/**", "src/x/y/z.py", True),
    ("src/**", "src", False),
    ("src/**", "srcx/a.py", False),
    ("src/**/a.py", "src/a.py", True),          # '**' matches zero depth
    ("src/**/a.py", "src/x/y/a.py", True),
    ("**/a.py", "a.py", True),
    ("**/a.py", "x/y/a.py", True),
    ("**", "anything/at/all", True),
    ("src/*.py", "src/a.py", True),
    ("src/*.py", "src/x/a.py", False),          # '*' never crosses a slash
    ("src/*", "src/.hidden", True),             # gitignore-like: '*' matches dotfiles
    ("*.md", "README.md", True),
    ("*.md", "docs/a.md", False),
    ("docs/*.md", "docs/a.md", True),
    ("tests/war.test.ts", "tests/war.test.ts", True),   # exact file
    ("tests/war.test.ts", "tests/war.test.tsx", False),
    ("src/a.py", "src/a.pyc", False),
    ("src/a?.py", "src/a1.py", True),
    ("src/a?.py", "src/a/.py", False),
    ("src/a.py", "SRC/A.PY", True),              # folded
    ("src/a.b", "src/aXb", False),               # '.' is literal, not regex
]

OVERLAP_TABLE = [
    # a, b, overlap?
    (["src/a.py"], ["src/b.py"], False),          # literal vs literal, differ
    (["src/a.py"], ["src/a.py"], True),
    (["src/a.py"], ["src/**"], True),
    (["src/*.ts"], ["src/*.css"], True),          # conservative prefix rule
    (["src/a/**"], ["src/b/**"], False),
    (["SRC/A.PY"], ["src/a.py"], True),           # case alias
    (["**"], ["docs/x.md"], True),
    (["src/*.ts"], ["lib/*.ts"], False),
    (["src"], ["src/a.py"], False),               # both literal, differ
    (["src/**"], ["src"], True),                  # prefix 'src' equals literal 'src'
    (["src/engine/**"], ["src/**"], True),
    (["tests/war.test.ts", "docs/*.md"], ["src/**"], False),
]


class MatchTests(unittest.TestCase):
    def test_table(self):
        for pat, path, want in MATCH_TABLE:
            with self.subTest(pat=pat, path=path):
                self.assertEqual(scope.matches([pat], path), want)

    def test_any_of_many(self):
        self.assertTrue(scope.matches(["docs/*.md", "src/**"], "src/x.py"))
        self.assertFalse(scope.matches([], "src/x.py"))

    def test_nfc_alias_matches(self):
        composed = unicodedata.normalize("NFC", "café.py")
        decomposed = unicodedata.normalize("NFD", "café.py")
        self.assertNotEqual(composed, decomposed)
        self.assertTrue(scope.matches([composed], decomposed))


class OverlapTests(unittest.TestCase):
    def test_table(self):
        for a, b, want in OVERLAP_TABLE:
            with self.subTest(a=a, b=b):
                self.assertEqual(bool(scope.overlaps(a, b)), want)
                self.assertEqual(bool(scope.overlaps(b, a)), want)

    def test_reports_original_pairs(self):
        self.assertEqual(scope.overlaps(["SRC/A.PY", "docs/x"], ["src/a.py"]), [("SRC/A.PY", "src/a.py")])

    def test_nfc_alias_overlaps(self):
        composed = unicodedata.normalize("NFC", "café/**")
        decomposed = unicodedata.normalize("NFD", "café/x.py")
        self.assertTrue(scope.overlaps([composed], [decomposed]))

    def test_literal_prefix(self):
        for pat, want in [("src/a.py", "src/a.py"), ("src/*.ts", "src"), ("src/**", "src"),
                          ("**/a.py", ""), ("**", ""), ("a/b/c?/d", "a/b")]:
            with self.subTest(pat=pat):
                self.assertEqual(scope.literal_prefix(pat), want)

    def test_resource_equality_after_fold(self):
        self.assertEqual(scope.resource_overlaps(["port:5199", "db:x"], ["PORT:5199"]), ["port:5199"])
        self.assertEqual(scope.resource_overlaps(["port:5199"], ["port:5200"]), [])

    def test_fold(self):
        self.assertEqual(scope.fold("SRC/Å.py"), "src/å.py")


if __name__ == "__main__":
    unittest.main()
