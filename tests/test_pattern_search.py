"""The Rust skill matcher answers exactly as `re` does, and is never required.

`skill_vocab.find_skills` reads every line of every posting against a whole vocabulary,
and a filter excludes on what it finds, so the Rust matcher (`rust/pattern-search-rs`,
installed by `make rust`) has to agree with `re` everywhere, not nearly. Rust's own case
folding and `\\s` do not: its `(?i)i` misses the dotted and dotless i, its `\\s` lacks
`\\x1c`-`\\x1f`. Without the matcher installed, the tests that need it skip and
`find_skills` asks `re`, as it always did. `PatternSet`'s fallbacks are
`tests/test_pattern_set.py`.
"""

from __future__ import annotations

import json
import re
import string
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from packages.matching import pattern_set, skill_vocab
from packages.matching.pattern_search import searches
from packages.matching.pattern_set import inline_pattern
from packages.matching.skill_vocab import CODE_SKILLS, SKILLS, Skill, find_skills

VOCABULARIES = {"SKILLS": SKILLS, "CODE_SKILLS": CODE_SKILLS}
GOLDEN = Path(__file__).parent / "fixtures" / "golden" / "postings.json"
#: The characters `re.IGNORECASE` pairs with an ASCII letter on Python 3.12.
CASE_PARTNERS = "\u0130\u0131\u212a\u017f"
WHITESPACE_CATEGORIES = {"Zs", "Zl", "Zp", "Cc", "Cf"}


def by_re(line: str, vocabulary: tuple[Skill, ...]) -> list[str]:
    return [skill.key for skill in vocabulary if skill.pattern.search(line)]


def keys(skills: list[Skill]) -> list[str]:
    return [skill.key for skill in skills]


def golden_lines() -> list[str]:
    """Every line and sentence of the twelve real crawled postings."""
    postings = json.loads(GOLDEN.read_text())["postings"]
    lines: dict[str, None] = {}
    for posting in postings:
        text = posting.get("description_raw") or posting.get("description") or ""
        for line in text.splitlines():
            lines.setdefault(line, None)
            for sentence in re.split(r"(?<=[.;])\s+", line):
                lines.setdefault(sentence, None)
    return [line for line in lines if line.strip()]


def every_character() -> str:
    return "".join(chr(c) for c in range(0x110000) if not 0xD800 <= c <= 0xDFFF)


def test_the_golden_postings_give_real_lines_to_test_against() -> None:
    lines = golden_lines()

    assert len(lines) > 200
    assert any(by_re(line, SKILLS) for line in lines)


# --------------------------------------------------------------------------
# The reference: `re.search` over the syntax the matcher takes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", VOCABULARIES)
def test_every_vocabulary_pattern_is_in_the_syntax_the_matcher_takes(name: str) -> None:
    for skill in VOCABULARIES[name]:
        pattern = inline_pattern(skill.pattern)

        assert pattern is not None, skill.key
        searches(pattern, "")


@pytest.mark.parametrize(
    ("pattern", "position"),
    [
        ("./x/**", 5),
        ("(?i)\u00e9", 4),
        ("[a-]", 3),
        ("(?<!a|b)c", 5),
        (r"\bgo", 0),
        ("[a&&b]", 2),
        ("(?=a)?", 5),
        ("a)", 1),
        ("(?:)+(?!a)", 4),
        ("(?:(?=a))*b", 9),
    ],
)
def test_syntax_outside_it_is_refused_where_it_starts(pattern: str, position: int) -> None:
    with pytest.raises(ValueError, match=f"at position {position}$"):
        searches(pattern, "x")


def test_inside_it_searches_is_re_search() -> None:
    cases = [
        (r"(?i)(?<![A-Za-z0-9+#])(?:Python)(?![A-Za-z0-9+#]|\.[A-Za-z])", "Python, Go"),
        (r"Java(?!\s*Script)", "Java Script"),
        (r"(?i)(?-i:REST)", "rest"),
        ("a.c", "a\nc"),
        ("", ""),
    ]

    for pattern, line in cases:
        assert searches(pattern, line) == (re.search(pattern, line) is not None), pattern


# --------------------------------------------------------------------------
# find_skills without the matcher
# --------------------------------------------------------------------------


def test_without_the_rust_matcher_find_skills_asks_re(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pattern_set, "_BATCH_CLASS", None)
    monkeypatch.setattr(skill_vocab, "_SETS", {})

    for line in golden_lines():
        assert keys(find_skills(line)) == by_re(line, SKILLS), line


def test_a_vocabulary_given_as_a_list_is_read_by_re() -> None:
    line = "Python, Go and Kubernetes"

    assert keys(find_skills(line, list(SKILLS))) == by_re(line, SKILLS)


# --------------------------------------------------------------------------
# The matcher, when installed
# --------------------------------------------------------------------------


def test_find_skills_uses_the_rust_matcher_when_it_is_installed(rust_batch_class: Any) -> None:
    find_skills("Python")

    assert pattern_set._BATCH_CLASS is rust_batch_class
    assert skill_vocab._SETS[id(SKILLS)][1].native


@pytest.mark.parametrize("name", VOCABULARIES)
def test_the_rust_matcher_agrees_with_re_on_real_postings(rust_batch_class: Any, name: str) -> None:
    vocabulary = VOCABULARIES[name]
    for line in golden_lines():
        assert keys(find_skills(line, vocabulary)) == by_re(line, vocabulary), line


def test_case_partners_are_the_running_pythons(rust_batch_class: Any) -> None:
    """Every character `re` folds onto an ASCII letter, against every ASCII letter."""
    letters = string.ascii_letters
    folded = set(re.findall("(?i)[a-z]", every_character())) | set(CASE_PARTNERS)
    batch = rust_batch_class([f"(?i){x}" for x in letters] + [f"(?i)[{x}]" for x in letters])

    for char in sorted(folded):
        expected = [re.search(f"(?i){x}", char) is not None for x in letters]
        assert batch.run(char) == expected * 2, hex(ord(char))


def test_whitespace_is_the_running_pythons(rust_batch_class: Any) -> None:
    batch = rust_batch_class([r"\s", r"(?i)a\sb"])
    candidates = [c for c in every_character() if unicodedata.category(c) in WHITESPACE_CATEGORIES]

    for char in candidates:
        is_space = re.search(r"\s", char) is not None
        assert batch.run(char) == [is_space, False], hex(ord(char))
        assert batch.run(f"a{char}b") == [is_space, is_space], hex(ord(char))


def test_a_vocabulary_the_matcher_refuses_is_read_by_re(rust_batch_class: Any) -> None:
    odd = (Skill("word", "word", re.compile(r"\bword\b")), *SKILLS[:3])
    line = "a word in Python"

    assert keys(find_skills(line, odd)) == by_re(line, odd)
    assert not skill_vocab._SETS[id(odd)][1].native


def test_a_lone_surrogate_is_read_by_re(rust_batch_class: Any) -> None:
    line = "Python \ud800 Go, Rust"

    assert keys(find_skills(line)) == by_re(line, SKILLS)
