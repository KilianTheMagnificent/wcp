"""Path-pattern matching and conservative overlap detection (NFC + casefold)."""
from __future__ import annotations

import functools
import re
import unicodedata

_WILD = ("*", "?")


def fold(s: str) -> str:
    return unicodedata.normalize("NFC", s).casefold()


def _component(comp: str) -> str:
    out = []
    for ch in comp:
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


@functools.lru_cache(maxsize=1024)
def compile_pattern(pat: str) -> re.Pattern:
    """gitignore-like: '**' any depth incl. zero, '*' no slash, '?' one char; anchored; folded."""
    comps = fold(pat).split("/")
    out = ["^"]
    last = len(comps) - 1
    for i, comp in enumerate(comps):
        if comp == "**":
            out.append(".*" if i == last else "(?:[^/]+/)*")
        else:
            out.append(_component(comp))
            if i != last:
                out.append("/")
    out.append("$")
    return re.compile("".join(out))


def matches(patterns: list[str], path: str) -> bool:
    folded = fold(path)
    return any(compile_pattern(p).match(folded) for p in patterns)


def literal_prefix(pat: str) -> str:
    """The path up to (not including) the first component holding a wildcard."""
    keep = []
    for comp in pat.split("/"):
        if any(w in comp for w in _WILD):
            break
        keep.append(comp)
    return "/".join(keep)


def _is_literal(pat: str) -> bool:
    return not any(w in pat for w in _WILD)


def _under_or_equal(x: str, y: str) -> bool:
    return x == y or y == "" or x.startswith(y + "/")


def overlaps(a: list[str], b: list[str]) -> list[tuple[str, str]]:
    """Offending (pa, pb) pairs under the spec's conservative prefix rule."""
    out = []
    for pa in a:
        fa = fold(pa)
        for pb in b:
            fb = fold(pb)
            if _is_literal(fa) and _is_literal(fb):
                if fa == fb:
                    out.append((pa, pb))
                continue
            xa, xb = literal_prefix(fa), literal_prefix(fb)
            if _under_or_equal(xa, xb) or _under_or_equal(xb, xa):
                out.append((pa, pb))
    return out


def resource_overlaps(a: list[str], b: list[str]) -> list[str]:
    folded_b = {fold(r) for r in b}
    return [r for r in a if fold(r) in folded_b]
