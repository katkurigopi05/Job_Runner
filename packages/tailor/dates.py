"""What a date looks like on a résumé. One answer, for every module that asks.

There were three. `assemble.py` had a pattern for laying entries out, `ats.py`
had one for deciding whether a section is dated, and `bullets.py` has a third
for telling a date line from a bullet. They disagreed, and the disagreement was
invisible until someone wrote a date the way a student writes one.

## What the disagreement cost

An internship is usually dated with a point rather than a range — `Summer
2024`, `Jun 2023` — and often with the end left off entirely, because the role
had not finished when the résumé was written. Against all three patterns that
was not a date:

- `assemble.py` rendered it as a bold **entry name**, so `Summer 2024` sat
  under the job title looking like a second employer. Three internships
  produced six entries.
- `ats.py` reported "no date range found anywhere in experience" on a résumé
  with a date on every entry, and charged 0.15 of the parse score for it.

Both are the same mistake: a range is one way to write a date, and the code
treated it as the only way.

## The vocabulary

- **Point** — `Jun 2024`, `June 2024`, `Summer 2024`. A moment, not a span.
  Normal for an internship and not a defect.
- **Range** — `Mar 2021 - Present`, `2017 - 2021`. A span.
- **Open** — `May 2024 -`. A separator with nothing after it. Distinct from a
  point *and* from a range: the writer meant a span and left the end off, so a
  parser has to guess whether the role is current. `is_open_ended` exists so
  that guess is surfaced rather than made silently.

A bare year is a date only when it is the whole line. As a trailing fragment
it is usually part of something else — `B.S. Computer Science, State
University, 2017` is one entry name, and splitting `2017` off it leaves a
name ending in a comma.
"""

from __future__ import annotations

import re

__all__ = [
    "contains_date",
    "date_only",
    "is_open_ended",
    "trailing_date",
]

MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
SEASONS = "spring|summer|fall|autumn|winter"
YEAR = r"(?:19|20)\d{2}"

#: A day of the month, with or without an ordinal suffix. `1`, `01st`, `15th`.
_DAY = r"\d{1,2}(?:st|nd|rd|th)?"

#: `Jun 2024`, `June 2024`, `Summer 2024`. A month or season with its year.
_POINT = rf"(?:(?:{MONTHS})\w*\.?|(?:{SEASONS}))\s+{YEAR}"

#: `01st Apr, 2021`, `12-JUN-2021`, `15 May 2021`. Day first.
_DMY = rf"{_DAY}[\s.-]*(?:{MONTHS})\w*\.?[\s,.-]*{YEAR}"

#: `Apr 01, 2021`, `May 15 2021`. Month first.
_MDY = rf"(?:{MONTHS})\w*\.?[\s.-]*{_DAY}[\s,.-]*{YEAR}"

#: Any single moment, however it is written.
_MOMENT = rf"(?:{_DMY}|{_MDY}|{_POINT})"

#: `September - November 2023`, `Jul-Aug 2022`, `Summer - Fall 2024`.
#:
#: The year is written once, at the end, because the whole thing happened
#: inside one year. That is how a short internship is nearly always dated, and
#: it was the shape that sent the owner's own Networking internship through as
#: a bold employer name: every other pattern here wants a year on both sides.
_SHARED_YEAR_RANGE = (
    rf"(?:(?:{MONTHS})\w*\.?|(?:{SEASONS}))\s*[-–—]\s*"
    rf"(?:(?:{MONTHS})\w*\.?|(?:{SEASONS}))\s+{YEAR}"
)

#: What may open a range: any moment, or a bare year.
_START = rf"(?:{_MOMENT}|{YEAR})"

#: What may close one. `present`/`current` because a job in progress says so.
_END = rf"(?:{_MOMENT}|{YEAR}|present|current|now|ongoing|date)"

#: A hyphen, dash, or the word "to".
_SEP = r"\s*(?:[-–—]|\bto\b)\s*"

_RANGE = rf"{_START}{_SEP}{_END}"

#: A range whose end is missing — the separator is there and nothing follows.
_OPEN = rf"{_START}\s*[-–—]"

#: `Summer 2023, Summer 2024` — repeat internships at one employer.
_POINT_LIST = rf"{_POINT}(?:\s*(?:,|&|and)\s*{_POINT})+"

# Order matters: the longest reading first, so `Jun 2024 - Aug 2024` is one
# range rather than a point followed by rubbish, and `September - November
# 2023` is one range rather than the point `November 2023` with a stray word
# in front of it.
_ANY = rf"(?:{_RANGE}|{_SHARED_YEAR_RANGE}|{_POINT_LIST}|{_OPEN}|{_MOMENT})"

#: The whole line is a date. A bare year counts here and only here.
_DATE_ONLY_RE = re.compile(rf"^\s*({_ANY}|{YEAR})\s*$", re.IGNORECASE)

#: A date at the end of a line that has something else in front of it.
#: Bare years are deliberately excluded — see the module docstring.
_TRAILING_DATE_RE = re.compile(rf"\s+({_ANY})\s*$", re.IGNORECASE)

#: Anywhere in a passage. Used to answer "is this section dated at all".
_ANY_DATE_RE = re.compile(_ANY, re.IGNORECASE)

_OPEN_ONLY_RE = re.compile(rf"^\s*{_OPEN}\s*$", re.IGNORECASE)


def date_only(line: str) -> str | None:
    """The date, when the line is nothing but one. Otherwise None."""
    match = _DATE_ONLY_RE.match(line)
    return match.group(1).strip() if match else None


def trailing_date(line: str) -> tuple[str, str] | None:
    """Split `("Staff Engineer, Acme", "Mar 2021 - Present")`, or None.

    Only when something precedes the date — a line that is *only* a date is
    `date_only`'s business, and answering both ways is how the month of
    `Mar 2021 - Present` ended up being treated as an employer.
    """
    # A line that is *entirely* a date has no name to split off, and the
    # trailing pattern will happily find one: its leading `\s+` matches the
    # space after the month, so `Mar 2021 - Present` comes back as the name
    # `Mar` with the date `2021 - Present`. That was the original defect, and
    # checking here rather than only at the call site keeps it fixed for the
    # next caller rather than for the one that happened to ask in order.
    if date_only(line) is not None:
        return None
    match = _TRAILING_DATE_RE.search(line)
    if not match:
        return None
    name = line[: match.start()].rstrip()
    if not name:
        return None
    return name, match.group(1).strip()


def contains_date(text: str) -> bool:
    """Whether any date appears. A point counts; it is still a date."""
    return _ANY_DATE_RE.search(text) is not None


def is_open_ended(line: str) -> bool:
    """A range whose end was left off — `May 2024 -`.

    Worth surfacing rather than silently repairing. A parser reading this has
    to decide whether the role is current, and the two readings put different
    things on an employer's screen; only the owner knows which is true, and
    §2.1 forbids guessing an end date on their behalf.
    """
    return _OPEN_ONLY_RE.match(line) is not None
