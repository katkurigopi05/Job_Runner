"""Which of several regular expressions a text contains: in one Rust pass when built.

The Rust matcher, `rust/pattern-search-rs` (`make rust`), reads a whole set of
patterns over a text in one `RegexSet` pass, with answers held identical to
`re`'s (`pattern_search.py` is the reference). `PatternSet` uses it when it is
installed and takes every pattern, and asks `re` otherwise: when the module is
missing or will not load, when a pattern is outside the syntax it takes or
carries a flag that cannot be written inline, for a line with a lone surrogate,
and if its lookaround engine hits its backtracking limit. Each of those gives
`re`'s answer, never none; the ones that should not happen are logged.

Used by `skill_vocab.find_skills` (a vocabulary per line) and
`eligibility.read_posting` (its topic words over a whole posting).
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Sequence
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: The flags a pattern can carry inline. `re.UNICODE` is every str pattern's default.
_INLINE_FLAGS: dict[int, str] = {re.UNICODE: "", re.UNICODE | re.IGNORECASE: "(?i)"}


def _native_batch_class() -> Any:
    """`pattern_search_rs.SearchesBatch` when `make rust` installed it, else None."""
    try:
        return importlib.import_module("pattern_search_rs").SearchesBatch
    except ImportError as error:
        if error.name != "pattern_search_rs":  # installed, but it would not load
            log.warning("pattern_set.rust_matcher_unloadable", error=str(error))
        return None


_BATCH_CLASS = _native_batch_class()


def inline_pattern(pattern: re.Pattern[str]) -> str | None:
    """The pattern with its flags written inline, or None for a flag that cannot be."""
    prefix = _INLINE_FLAGS.get(pattern.flags)
    return None if prefix is None else prefix + pattern.pattern


def _new_batch(patterns: tuple[re.Pattern[str], ...]) -> Any:
    if _BATCH_CLASS is None:
        return None
    inline = [inline_pattern(pattern) for pattern in patterns]
    if None in inline:
        log.warning("pattern_set.rust_matcher_refused", reason="a flag that cannot be inline")
        return None
    batch = _BATCH_CLASS(inline)
    try:
        batch.run("")
    except (ValueError, RuntimeError) as refused:  # syntax it does not take; a compile limit
        log.warning("pattern_set.rust_matcher_refused", reason=str(refused))
        return None
    return batch


class PatternSet:
    """Fixed patterns, asked together of one text at a time."""

    def __init__(self, patterns: Sequence[re.Pattern[str]]) -> None:
        self.patterns = tuple(patterns)
        self._batch = _new_batch(self.patterns)

    @property
    def native(self) -> bool:
        """Whether the Rust matcher answers for this set."""
        return self._batch is not None

    def found(self, text: str) -> list[bool]:
        """Whether each pattern is found in `text`, in order, as `pattern.search` says."""
        if self._batch is not None:
            try:
                found: list[bool] = self._batch.run(text)
            except UnicodeEncodeError:  # a lone surrogate cannot cross into Rust
                pass
            except RuntimeError as error:  # the lookaround engine's backtracking limit
                log.warning("pattern_set.rust_matcher_failed", error=str(error))
            else:
                return found
        return [pattern.search(text) is not None for pattern in self.patterns]
