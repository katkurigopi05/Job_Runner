"""`loosened` gives a pattern that matches wherever the original does.

A screen built from it decides which descriptions are searched at all, so a loosened
pattern that missed one match would hide a remote job. Held three ways: each rewrite,
a seeded fuzz of patterns and texts against `re` itself, and the screens `locality`
uses being in the syntax the Rust matcher takes exactly.
"""

from __future__ import annotations

import random
import re

import pytest

from packages.matching import locality
from packages.matching.loosen import ANY, Unloosenable, loosened
from packages.matching.pattern_search import searches
from packages.matching.pattern_set import inline_pattern


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        (r"\bremote\b", "remote"),
        (r"a(?!b\b)c", "ac"),
        (r"(?<=x)y", "y"),
        (r"(?:^|\. )work", r"(?:|\. )work"),
        (r"[\w-]+", r"\S+"),
        (r"[\w'-]+", r"\S+"),
        (r"[ \t]+", r"\s+"),
        (r"[^.\n]{0,60}", ".{0,60}"),
        (r"[^.]*", ANY + "*"),
        (r"[- ]", r"(?:\-|\s)"),
        ("[]a]", r"(?:[a]|\])"),
        ("can\u2019t", r"can\St"),
        (r"a{,3}", "a{0,3}"),
        (r"a{2,5}?", "a{2,5}"),
        (r"a+?b", "a+b"),
        ("{x}", r"\{x\}"),
        (r"(?P<n>a)b", "(?:a)b"),
        (r"(?i:ab)(?-i:C)", "(?i:ab)(?-i:C)"),
        (r"(?=a)*b", "b"),
        (r"\d+\s\w", r"\S+\s\S"),
    ],
)
def test_each_rewrite(pattern: str, expected: str) -> None:
    assert loosened(re.compile(pattern)).pattern == expected


@pytest.mark.parametrize("pattern", [r"(a)\1", r"(?>a)", r"a*+", r"(a)?(?(1)b|c)", r"(?s:a.)"])
def test_what_cannot_be_widened_safely_is_refused(pattern: str) -> None:
    with pytest.raises(Unloosenable):
        loosened(re.compile(pattern))


def test_a_verbose_pattern_is_refused() -> None:
    with pytest.raises(Unloosenable):
        loosened(re.compile("a b", re.VERBOSE))


_ATOMS = [
    "a", "b", "k", "s", "i", " ", ".", r"\.", r"\s", r"\S", r"\w", r"\d", r"\b", r"\W", "-",
    "[ab]", "[^a]", "[^.\\n]", r"[\w-]", "[ \\t]", "[a-c]", "[]a]", "\u2019", "\u00e9", "^", "$",
]  # fmt: skip
_TEXT_CHARS = "aAbkKs\u017fi\u0131 .\n-\t\u2019\u00e9\u212a1_"


def _random_pattern(rng: random.Random, depth: int = 0) -> str:
    out = []
    for _ in range(rng.randint(0, 4)):
        if depth < 2 and rng.random() < 0.25:
            opener = rng.choice(["(", "(?:", "(?=", "(?!", "(?<=a", "(?<!b", "(?i:"])
            inner = "a" if opener.startswith("(?<") else _random_pattern(rng, depth + 1)
            piece = opener + ("" if opener.startswith("(?<") else inner) + ")"
            piece += "|" + _random_pattern(rng, depth + 1) if rng.random() < 0.2 else ""
        else:
            piece = rng.choice(_ATOMS)
        out.append(piece + rng.choice(["", "", "?", "*", "+", "{0,2}", "{1,3}", "*?", "{,2}"]))
    return "".join(out)


def test_a_loosened_pattern_matches_wherever_the_original_does() -> None:
    rng = random.Random(20261127)
    checked = 0
    for _ in range(6000):
        text = rng.choice(["(?i)", ""]) + _random_pattern(rng)
        try:
            original = re.compile(text)
        except re.error:
            continue
        try:
            loose = loosened(original)
        except Unloosenable:
            continue
        for _ in range(12):
            line = "".join(rng.choice(_TEXT_CHARS) for _ in range(rng.randint(0, 10)))
            if original.search(line):
                assert loose.search(line), (text, loose.pattern, line)
                checked += 1
    assert checked > 5000


def test_the_remote_screens_are_in_the_syntax_the_matcher_takes() -> None:
    for pattern in (
        locality._REMOTE_DENIAL_RE,
        locality._REMOTE_CONDITIONAL_RE,
        locality._REMOTE_DECLARED_RE,
    ):
        inline = inline_pattern(loosened(pattern))

        assert inline is not None
        searches(inline, "")
