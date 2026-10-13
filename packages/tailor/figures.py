"""The numbers a bullet states, and whether a rewrite kept them.

The fabrication guard protects a number in one direction. It refuses a
rewrite that says `40%` when the source does not, and it has nothing to say
about a rewrite that leaves the source's `40%` out. Tailoring the owner's
résumé for one posting on 2026-10-13, eight rewrites were accepted and two of
them had done exactly that: one lost a model's test score and the span of its
data, one lost a count. Nothing was invented, so nothing was refused, and the
bullets that came out said less than the ones that went in.

`technologies.py` is the same idea for a library name, and this sits beside
it in `rewrite.vet` for the same reason: every other check there reads the
output, and these two read what is missing from it.

## What a figure is

A value, not a spelling. `1,000`, `1000` and `1k` are one figure; `30%`,
`30 %` and `30 percent` are one; `12 million`, `12M` and `12,000,000` are
one. `30%` and `30` are two, and so are `12 million` and `12`: a figure that
lost its sign or its magnitude was changed, not reformatted.

A unit is not part of the value. `800ms` and `800 ms` are the same figure,
and so, here, are `800 ms` and `800 s`. A unit written against its number is
already one token to the fabrication guard, which refuses the second as a
number the source does not have.

## What is not one

- **A date.** The guard reads a bare 19xx or 20xx as a year
  (`guard._YEAR_RE`) and this reads it the same way, so one token is never a
  year to one check and a metric to the other. A rewrite may leave a date
  out; whether it may *add* one is the guard's question and unchanged. A
  count that happens to fall in that range is read as a count when the text
  marks it as one: `2,000`, `2000+`, `over 2000`, `2000 requests per second`.
  A bare `2000 requests` is not marked and reads as a year, as it does to the
  rest of the guard.
- **A phone number, an address, a link, a page number.** None is in a bullet
  on a well-formed résumé. They are masked anyway, because a line classified
  wrongly upstream should not become a metric here.
- **A digit inside a name.** `EC2`, `S3`, `k8s`, `GPT-4`, `COVID-19`. Names
  are held by the checks that read names.
- **A number written as a word in the source.** "one of three teams" has no
  figure. In the *rewrite* a number word does count, so `3 services` may
  become `three services`.

The message names the figure as the bullet wrote it. The figure is the
owner's, so `loggable` is the same statement without it (§10).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from packages.tailor.guard import _NUMBER_WORDS, _YEAR_RE

_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_YEAR = r"(?:19|20)\d{2}"

#: Spans that hold digits and state no metric. Blanked before anything is
#: read, in this order: an address can hold what looks like a date.
_NOT_FIGURES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # Links and addresses.
        r"\b(?:https?://|www\.)\S+",
        r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+",
        r"\b[\w-]+(?:\.[\w-]+)*\.(?:com|org|net|io|dev|ai|app|edu|gov|co)\b(?:/\S*)?",
        # Phone numbers: +1 415-555-0134, (415) 555-0134, 415.555.0134.
        r"(?<![\w.])\+\d[\d\s().-]{8,}\d",
        r"(?<![\w.])(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?!\w)",
        # Page numbers.
        r"\bpage\s+\d+(?:\s+of\s+\d+)?\b",
        r"\bpp?\.\s?\d+(?:\s?[-–—]\s?\d+)?",
        # Dates written with digits alone.
        rf"\b{_YEAR}-\d{{1,2}}(?:-\d{{1,2}})?\b",
        r"\b\d{1,2}/\d{1,2}/(?:\d{4}|\d{2})\b",
        rf"\b\d{{1,2}}/{_YEAR}\b",
        # A month with its year, and the day between or before them.
        rf"\b{_MONTH}\s+(?:\d{{1,2}}(?:st|nd|rd|th)?,?\s+)?(?:{_YEAR}|'\d{{2}})\b",
        rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}\s+(?:{_YEAR}|'\d{{2}})\b",
        # A year abbreviated to its last two digits.
        r"(?<!\w)'\d{2}\b",
    )
)

#: One number: digits with thousands separators, a decimal, or a decimal with
#: no leading zero. Not inside a word (`EC2`), not the tail of a name joined
#: by a hyphen (`GPT-4`), and not the tail of a longer number.
_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_])(?<![A-Za-z]-)(?<!\d[.,])"
    r"(?P<digits>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
)
_PERCENT = re.compile(r"\s?(?:%|percent\b|pct\b)", re.IGNORECASE)
#: A magnitude written against the number. Upper-case `M` and `B` only: `5m`
#: is five minutes or five metres at least as often as five million. Not
#: followed by a letter, so `500MB` is five hundred of a unit.
_MAGNITUDE_SUFFIX = re.compile(r"(?:[kK]|MM|M|B|bn)(?![A-Za-z])")
_MAGNITUDE_WORD = re.compile(r"\s(thousand|million|billion|trillion)\b", re.IGNORECASE)
_MAGNITUDES = {
    "k": 3,
    "m": 6,
    "mm": 6,
    "b": 9,
    "bn": 9,
    "thousand": 3,
    "million": 6,
    "billion": 9,
    "trillion": 12,
}
_UNIT = re.compile(r"[A-Za-z]+")
#: What joins the two ends of a range. `5-10%` is 5% to 10%.
_RANGE_JOIN = re.compile(r"\s*(?:[-–—]|to|through)\s*", re.IGNORECASE)

#: Before a number that looks like a year, these say it is a count.
_COUNT_BEFORE = re.compile(
    r"(?:\b(?:over|about|around|approximately|approx\.?|nearly|almost|roughly|exceeding"
    r"|than|least|up\s+to)\s+|[~≈><$€£#]\s?)$",
    re.IGNORECASE,
)
#: How far back those are looked for. "approximately " is the longest.
_COUNT_WINDOW = 24
#: And after it: `2000+`, `2000 requests per second`, `2000/day`.
_COUNT_AFTER = re.compile(r"\s?\+|(?:\s+[\w-]+){0,2}\s+per\b|\s?/\s?[A-Za-z]", re.IGNORECASE)

_WORD = re.compile(r"[A-Za-z]+")


@dataclass(frozen=True)
class Figure:
    """One number a line states."""

    #: As the line wrote it: `1,000`, `30%`, `12 million`, `800ms`.
    text: str
    value: Decimal
    percent: bool = False

    @property
    def key(self) -> tuple[Decimal, bool]:
        """What two spellings of one figure share."""
        return (self.value, self.percent)


@dataclass(frozen=True)
class Loss:
    """The figures a rewrite left out, and what it says in their place."""

    #: As the bullet wrote them, in its order.
    dropped: tuple[str, ...]
    #: Figures the rewrite has that the bullet does not. With any, the bullet's
    #: were not left out but altered.
    instead: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        noun = "figure" if len(self.dropped) == 1 else "figures"
        named = ", ".join(repr(text) for text in self.dropped)
        if self.instead:
            said = ", ".join(repr(text) for text in self.instead)
            return f"changes the {noun} {named}: the rewrite says {said}"
        pronoun = "it" if len(self.dropped) == 1 else "them"
        return f"drops the {noun} {named} — the bullet states {pronoun} and the rewrite does not"

    @property
    def loggable(self) -> str:
        """`reason` without the figures, which are résumé content (§10)."""
        noun = "figure" if len(self.dropped) == 1 else "figures"
        verb = "changes" if self.instead else "drops"
        return f"{verb} {len(self.dropped)} {noun} the bullet states"


def _masked(text: str) -> str:
    """`text` with every span that is not a metric blanked, length kept."""
    for pattern in _NOT_FIGURES:
        text = pattern.sub(lambda match: " " * len(match.group(0)), text)
    return text


def _is_a_year(text: str, start: int, end: int, digits: str) -> bool:
    """Whether a bare number is a date and not a count.

    The guard's own reading of a year, less the cases where the words around
    it say it is being counted.
    """
    if not _YEAR_RE.match(digits):
        return False
    before = text[max(0, start - _COUNT_WINDOW) : start]
    counted = _COUNT_BEFORE.search(before) or _COUNT_AFTER.match(text, end)
    return counted is None


def read(text: str) -> list[Figure]:
    """The figures `text` states, in order."""
    masked = _masked(text)
    #: Each figure with where it stands and the power of ten it was scaled by.
    found: list[tuple[Figure, int, int, int]] = []

    for match in _NUMBER.finditer(masked):
        digits = match.group("digits")
        end = match.end()
        try:
            value = Decimal(digits.replace(",", ""))
        except InvalidOperation:
            continue

        percent = _PERCENT.match(masked, end)
        suffix = _MAGNITUDE_SUFFIX.match(masked, end)
        word = _MAGNITUDE_WORD.match(masked, end)
        exponent = 0
        if percent is not None:
            end = percent.end()
        elif suffix is not None:
            exponent, end = _MAGNITUDES[suffix.group(0).lower()], suffix.end()
        elif word is not None:
            exponent, end = _MAGNITUDES[word.group(1).lower()], word.end()
        else:
            unit = _UNIT.match(masked, end)
            if unit is not None:
                # `800ms`, `3D`, `10x`: the unit is shown, and is not the value.
                end = unit.end()
            elif _is_a_year(masked, match.start(), end, digits):
                continue

        figure = Figure(
            text=" ".join(text[match.start() : end].split()),
            value=value.scaleb(exponent).normalize(),
            percent=percent is not None,
        )
        found.append((figure, match.start(), end, exponent))

    # `5-10%` and `10-15 million`: the sign or magnitude is written once, on
    # the far end, and belongs to both.
    figures: list[Figure] = []
    for index, (figure, _, end, exponent) in enumerate(found):
        if index + 1 < len(found) and not figure.percent and not exponent:
            following, start, _, shared = found[index + 1]
            if (following.percent or shared) and _RANGE_JOIN.fullmatch(masked, end, start):
                figure = Figure(
                    text=figure.text,
                    value=figure.value.scaleb(shared).normalize(),
                    percent=following.percent,
                )
        figures.append(figure)
    return figures


def stated(text: str) -> set[tuple[Decimal, bool]]:
    """Every value `text` states, counting a number written as a word."""
    stated = {figure.key for figure in read(text)}
    for word in _WORD.findall(text):
        digits = _NUMBER_WORDS.get(word.lower())
        if digits is not None:
            stated.add((Decimal(digits).normalize(), False))
    return stated


def lost(original: str, candidate: str) -> Loss | None:
    """What `candidate` did to the figures `original` states, if anything."""
    kept = stated(candidate)
    dropped: list[str] = []
    seen: set[tuple[Decimal, bool]] = set()
    source = read(original)
    for figure in source:
        if figure.key in kept or figure.key in seen:
            continue
        seen.add(figure.key)
        dropped.append(figure.text)
    if not dropped:
        return None

    ours = {figure.key for figure in source}
    instead: list[str] = []
    for figure in read(candidate):
        if figure.key not in ours and figure.text not in instead:
            instead.append(figure.text)
    return Loss(dropped=tuple(dropped), instead=tuple(instead))


def dropped(original: str, candidate: str) -> tuple[str, ...]:
    """The figures `original` states and `candidate` does not, as written."""
    loss = lost(original, candidate)
    return loss.dropped if loss is not None else ()


__all__ = ["Figure", "Loss", "dropped", "lost", "read", "stated"]
