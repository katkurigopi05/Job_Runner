"""`PatternSet`: which of several patterns a text contains, in Rust when built, else `re`.

The answer must be `re`'s either way: `skill_vocab` and `eligibility` both decide
what a filter excludes on it. These tests hold the fallbacks; the matcher's
exactness is `tests/test_pattern_search.py`.
"""

from __future__ import annotations

import importlib
import re
from typing import Any

import pytest
import structlog.testing

from packages.matching import pattern_set
from packages.matching.pattern_set import PatternSet, inline_pattern

PATTERNS = (
    re.compile(r"sponsor", re.I),
    re.compile(r"(?<![A-Za-z])Go(?=\s*,)"),
    re.compile(r"citizen|green\s+card", re.I),
)
LINES = [
    "",
    "We cannot sponsor visas.",
    "Go, Rust and Python",
    "Google",
    "US Citizens only",
    "a green\u3000card holder",
    "We do not \u017fponsor",
    "Go\x1c, again",
]


def by_re(line: str) -> list[bool]:
    return [pattern.search(line) is not None for pattern in PATTERNS]


def test_a_flag_that_cannot_be_written_inline_has_no_pattern() -> None:
    assert inline_pattern(re.compile("^x", re.MULTILINE)) is None
    assert inline_pattern(re.compile("x", re.I)) == "(?i)x"
    assert inline_pattern(re.compile("x")) == "x"


def test_without_the_rust_matcher_re_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pattern_set, "_BATCH_CLASS", None)
    found = PatternSet(PATTERNS)

    assert not found.native
    for line in LINES:
        assert found.found(line) == by_re(line), line


def _import_failing_with(error: ImportError) -> Any:
    def import_module(name: str) -> Any:
        raise error

    return import_module


def test_a_matcher_that_is_not_installed_is_not_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = ModuleNotFoundError("No module named 'pattern_search_rs'", name="pattern_search_rs")
    monkeypatch.setattr(importlib, "import_module", _import_failing_with(missing))

    with structlog.testing.capture_logs() as logged:
        assert pattern_set._native_batch_class() is None

    assert logged == []


def test_a_matcher_that_is_installed_but_will_not_load_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = ImportError("mis-aligned LINKEDIT string pool", name="pattern_search_rs._native")
    monkeypatch.setattr(importlib, "import_module", _import_failing_with(broken))

    with structlog.testing.capture_logs() as logged:
        assert pattern_set._native_batch_class() is None

    assert [entry["event"] for entry in logged] == ["pattern_set.rust_matcher_unloadable"]


class _UncompilableBatch:
    """A batch whose patterns pass the checker but fail to compile, as a size limit would."""

    def __init__(self, patterns: list[str]) -> None:
        self.patterns = patterns

    def run(self, line: str) -> list[bool]:
        raise RuntimeError("Compiled regex exceeds size limit")


def test_patterns_the_matcher_cannot_compile_are_read_by_re(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pattern_set, "_BATCH_CLASS", _UncompilableBatch)

    with structlog.testing.capture_logs() as logged:
        found = PatternSet(PATTERNS)

    assert not found.native
    assert [entry["event"] for entry in logged] == ["pattern_set.rust_matcher_refused"]
    assert found.found("We cannot sponsor") == by_re("We cannot sponsor")


def test_a_flag_that_cannot_be_inline_is_read_by_re(rust_batch_class: Any) -> None:
    found = PatternSet((*PATTERNS, re.compile("^x", re.MULTILINE)))

    assert not found.native
    assert found.found("a\nx") == [*by_re("a\nx"), True]


def test_syntax_the_matcher_refuses_is_read_by_re(rust_batch_class: Any) -> None:
    found = PatternSet((*PATTERNS, re.compile(r"\bgo\b")))

    assert not found.native
    assert found.found("let go now") == [*by_re("let go now"), True]


def test_the_rust_matcher_answers_as_re(rust_batch_class: Any) -> None:
    found = PatternSet(PATTERNS)

    assert found.native
    for line in LINES:
        assert found.found(line) == by_re(line), line


def test_a_lone_surrogate_is_read_by_re(rust_batch_class: Any) -> None:
    line = "We cannot sponsor \ud800 visas"

    assert PatternSet(PATTERNS).found(line) == by_re(line)
