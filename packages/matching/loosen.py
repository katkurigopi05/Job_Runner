"""A looser regular expression that matches wherever the original does: a screen.

A pattern the Rust matcher cannot take exactly (word boundaries, lookarounds, `\\w`) can
still be screened by it: `loosened` rewrites the pattern into the syntax the matcher does
take, by steps that can only add matches, and the original is then searched only where
the screen finds something. Because the screen comes from the pattern itself, a new
alternative is screened too; nothing has to be kept in step with it by hand.

Each step makes the language larger, never smaller:

- a word boundary, `^`, `$`, `\\A` and `\\Z` are dropped, and so is every lookaround,
  whole (dropping only part of a negative one would make it refuse more);
- `\\w`, `\\d` and a non-ASCII literal become `\\S`, which none of them can be outside of,
  and `\\n`, `\\t` and their kind become `\\s`;
- a class becomes the same class where the matcher takes it, else `\\s`, `\\S`, `.` or
  any character at all, whichever holds every member;
- a lazy quantifier becomes greedy (whether a match exists does not change), and a
  capturing or named group becomes a plain one.

Anything else (a backreference, a conditional, an atomic group) is refused with
`Unloosenable`, and the caller then has no screen.
"""

from __future__ import annotations

import re

#: Any character at all, newline included, in the syntax the matcher takes.
ANY = r"(?:.|\s)"
_WHITESPACE_ESCAPES = frozenset("ntrfv")
_ZERO_WIDTH_ESCAPES = frozenset("bBAZ")
_NON_SPACE_ESCAPES = frozenset("wd")
_ANY_ESCAPES = frozenset("WDS")
_CLASS_SPECIAL = frozenset("\\[]^-&~|")
_QUANTIFIERS = frozenset("?*+")
#: A repeat as Python reads one: `{m}`, `{m,n}`, `{m,}` and `{,n}`. Any other brace is itself.
_BOUNDED = re.compile(r"\{(\d*)(,?)(\d*)\}")
_LOOKAROUNDS = ("(?=", "(?!", "(?<=", "(?<!")
_PLAIN_GROUPS = ("(?:", "(?P<")


class Unloosenable(ValueError):
    """The pattern has a construct `loosened` cannot widen safely."""


def loosened(pattern: re.Pattern[str]) -> re.Pattern[str]:
    """A pattern with the same flags that matches wherever `pattern` matches."""
    if pattern.flags & re.VERBOSE:
        raise Unloosenable("a verbose pattern")
    try:
        return re.compile(_Loosener(pattern.pattern).run(), pattern.flags)
    except re.error as error:
        raise Unloosenable(str(error)) from error


class _Loosener:
    def __init__(self, text: str) -> None:
        self.text = text
        self.at = 0

    def run(self) -> str:
        prefix = ""
        if self.text.startswith("(?i)"):
            prefix, self.at = "(?i)", 4
        body = self._sequence(closing=False)
        if self.at < len(self.text):
            raise Unloosenable(f"unbalanced ) at position {self.at}")
        return prefix + body

    def _sequence(self, *, closing: bool) -> str:
        out: list[str] = []
        while self.at < len(self.text):
            char = self.text[self.at]
            if char == ")":
                if not closing:
                    raise Unloosenable(f"unbalanced ) at position {self.at}")
                self.at += 1
                return "".join(out)
            piece = self._piece(char)
            if not piece and self._at_quantifier():
                self._quantifier()  # a repeated assertion is an assertion, dropped with it
                continue
            out.append(piece)
        if closing:
            raise Unloosenable("unterminated group")
        return "".join(out)

    def _piece(self, char: str) -> str:
        if char == "(":
            return self._group()
        if char == "[":
            return self._class()
        if char == "\\":
            return self._escape()
        if char in "^$":
            self.at += 1
            return ""
        if char in _QUANTIFIERS or char == "{":
            return self._quantifier()
        self.at += 1
        if char in ".|":
            return char
        if char in "]}":
            return "\\" + char
        if not char.isascii():
            return r"\s" if char.isspace() else r"\S"
        return char

    def _repeat(self) -> re.Match[str] | None:
        found = _BOUNDED.match(self.text, self.at)
        return found if found is not None and (found.group(1) or found.group(3)) else None

    def _at_quantifier(self) -> bool:
        return self.at < len(self.text) and (
            self.text[self.at] in _QUANTIFIERS or self._repeat() is not None
        )

    def _quantifier(self) -> str:
        bounded = self._repeat() if self.text[self.at] == "{" else None
        if self.text[self.at] == "{" and bounded is None:
            self.at += 1
            return r"\{"  # Python reads a brace that starts no repeat as itself
        if bounded is not None:
            low, comma, high = bounded.groups()
            quantifier = "{" + (low or "0") + comma + high + "}"
            self.at = bounded.end()
        else:
            quantifier = self.text[self.at]
            self.at += 1
        if self.at < len(self.text) and self.text[self.at] == "?":
            self.at += 1  # lazy: whether a match exists is the same
        elif self.at < len(self.text) and self.text[self.at] == "+":
            raise Unloosenable(f"possessive quantifier at position {self.at}")
        return quantifier

    def _group(self) -> str:
        start = self.at
        if self.text.startswith(_LOOKAROUNDS, start):
            self.at += 4 if self.text.startswith(("(?<=", "(?<!"), start) else 3
            self._sequence(closing=True)
            return ""
        if self.text.startswith("(?#", start):
            end = self.text.find(")", start)
            if end < 0:
                raise Unloosenable("unterminated comment")
            self.at = end + 1
            return ""
        for scoped in ("(?i:", "(?-i:"):
            if self.text.startswith(scoped, start):
                self.at += len(scoped)
                return scoped + self._sequence(closing=True) + ")"
        if self.text.startswith("(?P<", start):
            close = self.text.find(">", start)
            if close < 0:
                raise Unloosenable("unterminated group name")
            self.at = close + 1
        elif self.text.startswith("(?:", start):
            self.at += 3
        elif self.text.startswith("(?", start):
            raise Unloosenable(f"group kind at position {start}")
        else:
            self.at += 1
        return "(?:" + self._sequence(closing=True) + ")"

    def _escape(self) -> str:
        if self.at + 1 >= len(self.text):
            raise Unloosenable("trailing backslash")
        escaped = self.text[self.at + 1]
        self.at += 2
        if escaped in _ZERO_WIDTH_ESCAPES:
            return ""
        if escaped in _NON_SPACE_ESCAPES:
            return r"\S"
        if escaped in _ANY_ESCAPES:
            return ANY
        if escaped in _WHITESPACE_ESCAPES or escaped == "s":
            return r"\s"
        if escaped.isascii() and not escaped.isalnum():
            return "\\" + escaped
        raise Unloosenable(f"escape \\{escaped} at position {self.at - 2}")

    def _class(self) -> str:
        """A class as members, widened to the narrowest form the matcher takes."""
        self.at += 1
        negated = self.at < len(self.text) and self.text[self.at] == "^"
        if negated:
            self.at += 1
        members: set[str] = set()
        spaces = nonspaces = wide = newline = False
        first = True
        while self.at < len(self.text) and (self.text[self.at] != "]" or first):
            first = False
            char = self.text[self.at]
            if char == "\\":
                escaped = self.text[self.at + 1] if self.at + 1 < len(self.text) else ""
                self.at += 2
                if escaped in _WHITESPACE_ESCAPES or escaped == "s":
                    spaces = True
                    newline = newline or escaped == "n"
                elif escaped in _NON_SPACE_ESCAPES:
                    nonspaces = True
                elif escaped.isascii() and not escaped.isalnum() and escaped:
                    members.add(escaped)
                else:
                    wide = True
                continue
            if (
                self.at + 2 < len(self.text)
                and self.text[self.at + 1] == "-"
                and self.text[self.at + 2] != "]"
            ):
                end = self.text[self.at + 2]
                if not (char.isascii() and end.isascii() and char <= end):
                    wide = True
                else:
                    members.update(chr(c) for c in range(ord(char), ord(end) + 1))
                self.at += 3
                continue
            self.at += 1
            if not char.isascii():
                nonspaces = nonspaces or not char.isspace()
                spaces = spaces or char.isspace()
            elif char.isspace():
                spaces = True
                newline = newline or char == "\n"
            else:
                members.add(char)
        if self.at >= len(self.text):
            raise Unloosenable("unterminated class")
        self.at += 1
        if negated:
            # A negated class that refuses a newline holds nothing `.` does not.
            return "." if newline else ANY
        return _class_superset(members, spaces=spaces, nonspaces=nonspaces, wide=wide)


def _class_superset(members: set[str], *, spaces: bool, nonspaces: bool, wide: bool) -> str:
    if wide:
        return ANY
    printable = {m for m in members if m.isascii() and m.isprintable() and not m.isspace()}
    if nonspaces or len(printable) != len(members):
        return ANY if spaces else r"\S"
    if not printable:
        return r"\s" if spaces else ANY
    plain = sorted(m for m in printable if m not in _CLASS_SPECIAL)
    special = sorted(m for m in printable if m in _CLASS_SPECIAL)
    parts = ["[" + "".join(plain) + "]"] if plain else []
    parts.extend("\\" + m for m in special)
    if spaces:
        parts.append(r"\s")
    return parts[0] if len(parts) == 1 else "(?:" + "|".join(parts) + ")"
