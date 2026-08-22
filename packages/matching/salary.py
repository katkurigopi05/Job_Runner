"""Compare what a posting advertises against what the profile expects.

`Profile.salary_expectation` has always been held and never read for anything
except being typed onto a form verbatim. Postings that state a range are
therefore scored identically whether they pay half what the owner asked for or
twice it, which is the one comparison a job seeker most wants made.

## This reports; it never answers

§2.2 keeps `salary_expectation` verbatim from the profile because a wrong
answer has consequences, and `answers.VERBATIM_KEYS` enforces that. Nothing
here goes near a form. It reads the posting, reads the profile, and produces a
finding for the owner — the same standing as the gap report and the legitimacy
tier.

## Silence is not a signal

Most postings state nothing, and a few states now require a range, so absence
carries no information about the employer. An unstated range yields
`Comparison.UNKNOWN` with zero weight rather than a penalty: a posting that
declines to say is not a worse posting.

## What it will not try to do

No currency conversion, no cost-of-living adjustment, no hourly-to-annual
inference beyond the obvious. Each is a guess that would produce a confident
number from an assumption the owner never made — and a wrong salary comparison
is the kind that gets someone to skip a job they wanted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

#: Currency symbols we can read. A posting in a currency not listed is
#: UNKNOWN rather than silently compared against a different one.
_CURRENCIES = {"$": "USD", "£": "GBP", "€": "EUR"}

#: "$120,000 - $160,000", "$120k-$160k", "120000 to 160000 USD".
_RANGE_RE = re.compile(
    r"(?P<sym>[$£€])?\s?(?P<low>\d{2,3}(?:,\d{3})?(?:\.\d+)?)\s?(?P<lowk>k\b)?"
    r"\s*(?:-|–|—|to)\s*"
    r"(?P<sym2>[$£€])?\s?(?P<high>\d{2,3}(?:,\d{3})?(?:\.\d+)?)\s?(?P<highk>k\b)?",
    re.I,
)

#: A single figure: "$145,000 per year", "up to $160k".
_SINGLE_RE = re.compile(
    r"(?P<sym>[$£€])\s?(?P<value>\d{2,3}(?:,\d{3})?(?:\.\d+)?)\s?(?P<k>k\b)?", re.I
)

#: Below this a figure is an hourly rate or a typo, not an annual salary.
MIN_PLAUSIBLE_ANNUAL = 10_000


class Comparison(StrEnum):
    ABOVE = "above"
    WITHIN = "within"
    BELOW = "below"
    #: Either side did not say, or said something we will not guess at.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Range:
    low: float
    high: float
    currency: str | None = None

    def __post_init__(self) -> None:
        if self.high < self.low:
            # Capture before assigning: writing low first makes the second
            # line read the value it just set, leaving both ends equal.
            low, high = self.high, self.low
            object.__setattr__(self, "low", low)
            object.__setattr__(self, "high", high)

    @property
    def midpoint(self) -> float:
        return (self.low + self.high) / 2


@dataclass(frozen=True)
class SalaryFinding:
    comparison: Comparison
    finding: str
    posting_range: Range | None = None
    expected: Range | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "comparison": self.comparison.value,
            "finding": self.finding,
            "posting_low": self.posting_range.low if self.posting_range else None,
            "posting_high": self.posting_range.high if self.posting_range else None,
        }


def _scale(raw: str, thousands: bool) -> float:
    value = float(raw.replace(",", ""))
    return value * 1000 if thousands else value


def parse_range(text: str) -> Range | None:
    """The first plausible annual figure or range in `text`, or None.

    A single figure becomes a zero-width range. That is not a fudge: "up to
    $160k" and "$160k-$160k" mean the same thing for a comparison, and
    inventing a spread around a number the posting did not give would be
    reading something into it.
    """
    if not text:
        return None

    match = _RANGE_RE.search(text)
    if match is not None:
        low = _scale(match.group("low"), bool(match.group("lowk")))
        high = _scale(match.group("high"), bool(match.group("highk")))
        # A range written "$120k - 160k" marks only the first as thousands.
        if bool(match.group("lowk")) != bool(match.group("highk")) and high < low:
            high *= 1000
        symbol = match.group("sym") or match.group("sym2")
        if low >= MIN_PLAUSIBLE_ANNUAL and high >= MIN_PLAUSIBLE_ANNUAL:
            return Range(low, high, _CURRENCIES.get(symbol or ""))

    single = _SINGLE_RE.search(text)
    if single is not None:
        value = _scale(single.group("value"), bool(single.group("k")))
        if value >= MIN_PLAUSIBLE_ANNUAL:
            return Range(value, value, _CURRENCIES.get(single.group("sym") or ""))

    return None


def compare(posting_text: str, expectation: str | None) -> SalaryFinding:
    """Where the posting's range sits against the owner's expectation."""
    if not expectation or not expectation.strip():
        return SalaryFinding(Comparison.UNKNOWN, "no salary expectation on the profile")

    wanted = parse_range(expectation)
    if wanted is None:
        # The owner wrote something we will not guess at — "competitive",
        # "negotiable", "DOE". Reading a number out of that is inventing one.
        return SalaryFinding(
            Comparison.UNKNOWN, f"expectation {expectation.strip()!r} is not a figure"
        )

    offered = parse_range(posting_text)
    if offered is None:
        return SalaryFinding(Comparison.UNKNOWN, "posting does not state a salary", expected=wanted)

    if offered.currency and wanted.currency and offered.currency != wanted.currency:
        # No conversion. A rate the owner never chose would produce a
        # confident comparison out of an assumption.
        return SalaryFinding(
            Comparison.UNKNOWN,
            f"posting is in {offered.currency}, expectation in {wanted.currency}; not converted",
            posting_range=offered,
            expected=wanted,
        )

    if offered.high < wanted.low:
        shortfall = (wanted.low - offered.high) / wanted.low
        return SalaryFinding(
            Comparison.BELOW,
            f"tops out {shortfall:.0%} below the bottom of what you asked for",
            posting_range=offered,
            expected=wanted,
        )

    if offered.low > wanted.high:
        return SalaryFinding(
            Comparison.ABOVE,
            "starts above the top of your range",
            posting_range=offered,
            expected=wanted,
        )

    return SalaryFinding(
        Comparison.WITHIN,
        "overlaps the range you asked for",
        posting_range=offered,
        expected=wanted,
    )
