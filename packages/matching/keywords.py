"""The feed's keywords: whole words, words near each other, and the words around a hit.

The filter was a substring test. Asked on 2026-10-20 for "software engineer
ai", it was wrong both ways it could be typed:

- **As one phrase** the role table read "software engineer" and the filter
  kept every software engineer title, so "ai" was dropped without a word:
  542 results, mostly not AI.
- **As two keywords** "ai" matched inside "maintain", "container" and
  "availability", and "software engineer" anywhere in a description: 199 of
  927 results only had the letters.

Now a keyword is matched as **whole words**, case aside, with a plural
allowed. A keyword of several words matches where it is written, and in a
title also where all its words sit **within two words of each other** in any
order ("AI Software Engineer", "Software Engineer, AI Platform"). Only in a
title: run over the live feed, nearness in a description found one company's
boilerplate ("open source software engineers and AI agents rely") on 20
unrelated postings, and words either side of a full stop. A keyword that is a
role name still finds the role's other titles ("Member of Technical Staff"),
and one that is a role name *plus* words finds those titles only when the
title also says the words. Every hit carries the two words either side of it,
so the feed can show why a posting matched.

Matching stays a text search over the title, location and description, as it
was: the role dropdown is the title-only filter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from packages.matching.roles import ROLE_ALIASES, canonical

#: Words a keyword's words may have between them in a title and still match:
#: "software engineer ai" matches "Software Engineer (AI Platform)" and "AI
#: Software Engineer". Elsewhere they must stand as written.
NEAR = 2

#: Words shown on each side of a hit.
CONTEXT = 2

#: A word, as a keyword and a posting are both read: letters and digits, dots
#: only between them ("node.js", "asp.net"), one leading dot (".net") and
#: trailing + or # ("c++", "c#"). A full stop ending a sentence is not part of
#: the word before it.
_WORD_RE = re.compile(r"\.?[a-z0-9](?:[a-z0-9]|\.(?=[a-z0-9]))*[+#]*", re.I)
_SPACE_RE = re.compile(r"\s+")

#: Characters read either side of a place a keyword might stand, when
#: confirming it in a field other than the title. A description is not split
#: into words whole: done for every posting, that made a keyword search take
#: 2-3.6s over the 4,543 postings in the feed, against 0.1s for the substring
#: test. A pattern finds the places first (`_phrase_pattern`), and a field
#: where a word is missing is ruled out before any of it is split.
_WINDOW = 160

#: Every role alias as words, longest first, so "member of technical staff"
#: wins over "staff" (which is not an alias, but the principle holds).
_ALIASES: tuple[tuple[tuple[str, ...], str], ...] = tuple(
    sorted(
        (
            (tuple(alias.split()), role)
            for role, aliases in ROLE_ALIASES.items()
            for alias in aliases
        ),
        key=lambda pair: -len(pair[0]),
    )
)


@dataclass(frozen=True)
class Token:
    word: str
    start: int
    end: int


def tokenize(text: str) -> list[Token]:
    return [Token(m.group(0).lower(), m.start(), m.end()) for m in _WORD_RE.finditer(text)]


def same(found: str, wanted: str) -> bool:
    """A word as written, or its plural."""
    return found in (wanted, f"{wanted}s", f"{wanted}es")


@lru_cache(maxsize=1024)
def _pattern(word: str) -> re.Pattern[str]:
    """Text where `word` may stand as a whole word, plural allowed.

    Looser than `tokenize` (it accepts "c" in "c++"), never stricter, so it can
    rule a field out but not in: a match is confirmed on the words around it.
    """
    return _phrase_pattern((word,))


@lru_cache(maxsize=256)
def _phrase_pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    """Text where `words` may stand in order, with nothing but spaces or
    punctuation between them ("C#.NET" has none). Like `_pattern`, looser than
    the words it stands for.

    The check on the character before comes *after* the first word, so the
    engine can jump to the word instead of trying every position: the same
    matches over the 4,372 live descriptions, 3-30x faster (kafka 0.34s to
    0.12s).
    """
    first = re.escape(words[0])
    parts = [rf"{first}(?<![a-z0-9]{first})(?:e?s)?"]
    parts.extend(rf"[^a-z0-9]*{re.escape(word)}(?:e?s)?" for word in words[1:])
    return re.compile("".join(parts) + r"(?![a-z0-9])", re.I)


def _tokens_between(text: str, lo: int, hi: int) -> list[Token]:
    """The words of `text[lo:hi]`, less any the window's edges cut in two."""
    found = _WORD_RE.finditer(text, lo, hi)
    tokens = [Token(m.group(0).lower(), m.start(), m.end()) for m in found]
    if tokens and lo > 0 and tokens[0].start == lo and _joins(text[lo - 1]):
        tokens = tokens[1:]
    if tokens and hi < len(text) and tokens[-1].end == hi and _joins(text[hi]):
        tokens = tokens[:-1]
    return tokens


def _joins(char: str) -> bool:
    return char.isalnum() or char in ".+#"


@dataclass(frozen=True)
class Keyword:
    """A keyword as typed, read into words and, if it names one, a role."""

    text: str
    words: tuple[str, ...]
    #: The role the keyword names, from the role table.
    role: str | None = None
    #: The keyword's words beyond the role's name: "ai" in "software engineer ai".
    extra: tuple[str, ...] = ()


@lru_cache(maxsize=256)
def parse(text: str) -> Keyword:
    words = tuple(token.word for token in tokenize(text))
    for alias, role in _ALIASES:
        size = len(alias)
        for start in range(len(words) - size + 1):
            if words[start : start + size] == alias:
                extra = words[:start] + words[start + size :]
                return Keyword(text=text, words=words, role=role, extra=extra)
    return Keyword(text=text, words=words)


@dataclass(frozen=True)
class Hit:
    """Where a keyword matched, with the words around it."""

    keyword: str
    #: "title", "location" or "description".
    field: str
    before: str
    match: str
    after: str


class _Posting(Protocol):
    title: str | None
    location: str | None
    description_raw: str | None


def _phrase(tokens: list[Token], words: tuple[str, ...]) -> tuple[int, int] | None:
    """The first place the words stand together in order."""
    size = len(words)
    for start in range(len(tokens) - size + 1):
        if all(same(tokens[start + k].word, words[k]) for k in range(size)):
            return start, start + size - 1
    return None


def _near(tokens: list[Token], words: tuple[str, ...]) -> tuple[int, int] | None:
    """The shortest stretch holding every word, if it is no longer than the
    words themselves plus `NEAR`."""
    wanted = set(words)
    positions = [
        (index, word)
        for index, token in enumerate(tokens)
        for word in wanted
        if same(token.word, word)
    ]
    best: tuple[int, int] | None = None
    counts: dict[str, int] = {}
    left = 0
    for right in range(len(positions)):
        counts[positions[right][1]] = counts.get(positions[right][1], 0) + 1
        while len(counts) == len(wanted):
            span = (positions[left][0], positions[right][0])
            if best is None or span[1] - span[0] < best[1] - best[0]:
                best = span
            word = positions[left][1]
            counts[word] -= 1
            if not counts[word]:
                del counts[word]
            left += 1
    if best is None or best[1] - best[0] + 1 > len(words) + NEAR:
        return None
    return best


def _clean(text: str) -> str:
    return _SPACE_RE.sub(" ", text).strip()


def _hit(keyword: str, field: str, text: str, tokens: list[Token], span: tuple[int, int]) -> Hit:
    first, last = span
    lo, hi = max(first - CONTEXT, 0), min(last + CONTEXT, len(tokens) - 1)
    # Whether the words shown reach the field's ends, read on the whole text
    # because `tokens` may be a window of it.
    at_start = _WORD_RE.search(text, 0, tokens[lo].start) is None
    at_end = _WORD_RE.search(text, tokens[hi].end) is None
    before = _clean(text[0 if at_start else tokens[lo].start : tokens[first].start])
    after = _clean(text[tokens[last].end : len(text) if at_end else tokens[hi].end])
    return Hit(
        keyword=keyword,
        field=field,
        before=before if at_start else f"… {before}",
        match=_clean(text[tokens[first].start : tokens[last].end]),
        after=after if at_end else f"{after} …",
    )


def _in_title(keyword: Keyword, title: str) -> Hit | None:
    if not all(_pattern(word).search(title) for word in keyword.words):
        return None
    tokens = tokenize(title)
    span = _phrase(tokens, keyword.words)
    if span is None and len(keyword.words) > 1:
        span = _near(tokens, keyword.words)
    return None if span is None else _hit(keyword.text, "title", title, tokens, span)


def _as_written(keyword: Keyword, field: str, text: str) -> Hit | None:
    """The first place `text` says the keyword's words in order, read only
    around the places it might."""
    for found in _phrase_pattern(keyword.words).finditer(text):
        lo = max(found.start() - _WINDOW, 0)
        hi = min(found.end() + _WINDOW, len(text))
        tokens = _tokens_between(text, lo, hi)
        span = _phrase(tokens, keyword.words)
        if span is not None:
            return _hit(keyword.text, field, text, tokens, span)
    return None


def find(keyword: Keyword, posting: _Posting) -> Hit | None:
    """Where the posting says the keyword, title first, or None."""
    if not keyword.words:
        return None
    title = posting.title or ""
    for field, text in (
        ("title", title),
        ("location", posting.location or ""),
        ("description", posting.description_raw or ""),
    ):
        hit = _in_title(keyword, text) if field == "title" else _as_written(keyword, field, text)
        if hit is not None:
            return hit
    if keyword.role is not None and canonical(title) == keyword.role:
        title_words = [token.word for token in tokenize(title)]
        if all(any(same(found, word) for found in title_words) for word in keyword.extra):
            return Hit(keyword.text, "title", "", _clean(title), "")
    return None


__all__ = ["CONTEXT", "NEAR", "Hit", "Keyword", "Token", "find", "parse", "same", "tokenize"]
