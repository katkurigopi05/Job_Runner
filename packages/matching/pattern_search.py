"""Whether a regular expression is found in a line: what `rust/pattern-search-rs` reproduces.

`skill_vocab.find_skills` asks this of every skill in a vocabulary for every line of every
posting, about 3,800 searches a posting, and they were 81% of `requirements.extract`.
The Rust crate answers it for a whole vocabulary in one pass over the line. This
module is the behavior it is held to: its recorded cases come from `searches`.

A pattern is Python syntax with its flags written inline, `(?i)` first for a
case-insensitive one, so one string carries everything `re.search` is told.

Only the syntax the vocabularies use is taken, so that the port can match `re` exactly
rather than nearly. Everything else is refused with a `ValueError` naming the position,
in the same place by both, and `skill_vocab` then asks `re` itself:

- literals; under `(?i)` only ASCII ones, whose case partners `re` decides (`k` also
  matches the Kelvin sign, `s` the long s, `i` the dotted and dotless i);
- `.`, `\\s`, `\\S`, and a backslash before ASCII punctuation or a space;
- a class of ASCII characters and ASCII letter or digit ranges, never negated, and
  without `&`, `~` or `|`, which Rust reads as set operations when doubled;
- `(...)`, `(?:...)`, `(?=...)`, `(?!...)`, `(?i:...)`, `(?-i:...)`, and `(?<=...)`,
  `(?<!...)` around a fixed run of single characters;
- greedy `?`, `*`, `+`, `{m}`, `{m,n}` and `{m,}` (at most 1000) after a character, or after
  a group with one in it (Rust's lookaround engine will not repeat a group that can only
  match nothing), and `|`;
- `(?i)` at the very start.
"""

from __future__ import annotations

import re
from typing import NoReturn

_SPECIAL = frozenset(".^$*+?{}[]\\|()")
_QUANTIFIERS = frozenset("?*+")
#: A bounded repeat as taken: `{m}`, `{m,n}`, `{m,}`; Python's `{,n}` is refused.
_REPEAT = re.compile(r"\{(\d+)(?:(,)(\d*))?\}")
_MAX_REPEAT = 1000
_PUNCTUATION = frozenset(chr(c) for c in range(0x20, 0x7F) if not chr(c).isalnum())
_NOT_IN_CLASS = frozenset("\\[^-&~|")
_GROUPS = ("(?<=", "(?<!", "(?-i:", "(?i:", "(?:", "(?=", "(?!", "(")
_LOOKBEHINDS = ("(?<=", "(?<!")
_LOOKAROUNDS = ("(?=", "(?!", *_LOOKBEHINDS)


def searches(pattern: str, line: str) -> bool:
    """Whether `pattern` is found anywhere in `line`. ValueError outside the syntax above."""
    _Checker(pattern).check()
    return re.search(pattern, line) is not None


class _Checker:
    """Walks a pattern once and raises at the first thing outside the syntax taken."""

    def __init__(self, pattern: str) -> None:
        self.text = pattern
        self.at = 0

    def check(self) -> None:
        ignore_case = self.text.startswith("(?i)")
        if ignore_case:
            self.at = 4
        self._alternation(ignore_case, lookbehind=False)
        if self.at < len(self.text):  # only a `)` with no group open stops early
            self._refuse()

    def _refuse(self) -> NoReturn:
        raise ValueError(f"unsupported pattern syntax at position {self.at}")

    def _peek(self) -> str | None:
        return self.text[self.at] if self.at < len(self.text) else None

    def _alternation(self, ignore_case: bool, *, lookbehind: bool) -> bool:
        """Whether any branch has a character in it."""
        consumes = self._sequence(ignore_case, lookbehind=lookbehind)
        while self._peek() == "|":
            if lookbehind:
                self._refuse()
            self.at += 1
            branch = self._sequence(ignore_case, lookbehind=lookbehind)
            consumes = consumes or branch
        return consumes

    def _sequence(self, ignore_case: bool, *, lookbehind: bool) -> bool:
        consumes = False
        while (char := self._peek()) is not None and char not in "|)":
            atom = self._atom(ignore_case, lookbehind=lookbehind)
            consumes = consumes or atom
            if self._peek() in _QUANTIFIERS or self._peek() == "{":
                if lookbehind or not atom:
                    self._refuse()
                self._quantifier()
                if self._peek() in _QUANTIFIERS or self._peek() == "{":
                    self._refuse()
        return consumes

    def _quantifier(self) -> None:
        if self._peek() != "{":
            self.at += 1
            return
        repeat = _REPEAT.match(self.text, self.at)
        if repeat is None:
            self._refuse()
        low, _, high = repeat.groups()
        top = int(high) if high else int(low)
        if int(low) > _MAX_REPEAT or top > _MAX_REPEAT or (high and int(low) > int(high)):
            self._refuse()
        self.at = repeat.end()

    def _atom(self, ignore_case: bool, *, lookbehind: bool) -> bool:
        """Consume one atom; whether it has a character in it, so a quantifier may follow."""
        char = self.text[self.at]
        if char == "(":
            if lookbehind:
                self._refuse()
            return self._group(ignore_case)
        if char == "[":
            self._class()
        elif char == "\\":
            escaped = self.text[self.at + 1] if self.at + 1 < len(self.text) else None
            if escaped not in ("s", "S") and escaped not in _PUNCTUATION:
                self._refuse()
            self.at += 2
        elif char == ".":
            self.at += 1
        elif char in _SPECIAL or (ignore_case and not char.isascii()):
            self._refuse()
        else:
            self.at += 1
        return True

    def _group(self, ignore_case: bool) -> bool:
        opener = next((o for o in _GROUPS if self.text.startswith(o, self.at)), None)
        if opener is None or (opener == "(" and self.text.startswith("(?", self.at)):
            self._refuse()
        self.at += len(opener)
        if opener == "(?i:":
            ignore_case = True
        elif opener == "(?-i:":
            ignore_case = False
        if opener in _LOOKBEHINDS and self._peek() in (None, ")"):
            self._refuse()
        consumes = self._alternation(ignore_case, lookbehind=opener in _LOOKBEHINDS)
        if self._peek() != ")":
            self._refuse()
        self.at += 1
        return consumes and opener not in _LOOKAROUNDS

    def _class(self) -> None:
        self.at += 1  # the `[`
        if self._peek() in (None, "]", "^"):
            self._refuse()
        while (char := self._peek()) != "]":
            if char is None or char in _NOT_IN_CLASS or not (0x20 <= ord(char) < 0x7F):
                self._refuse()
            self.at += 1
            if self._peek() == "-":
                self.at += 1
                end = self._peek()
                if end is None or not (char.isalnum() and end.isalnum() and end.isascii()):
                    self._refuse()
                if end < char:
                    self._refuse()
                self.at += 1
        self.at += 1  # the `]`
