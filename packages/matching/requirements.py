"""Structured requirements read out of a posting: pay, skills, education.

`Posting` held a title, a location and a description, so "at least $150k",
"requires Kubernetes" and "no degree needed" could only be answered by reading
the text — by a person, every time. This reads it once, at ingestion, and
keeps the sentence each value came from.

## Unknown stays unknown

Every value here can be absent, and absence is recorded rather than defaulted:

- **No pay stated** is `compensation: None`, never zero and never a guess.
  A figure with no stated period has `period: None`; it is not assumed annual.
  (`salary.py`, which the rubric uses, does assume annual for a figure over
  $10k. That is an opinion for a ranking dimension; this feeds hard filters,
  where a wrong assumption silently drops a job.)
- **A skill named outside any requirements heading** is `unclassified`, not
  preferred and not required. "We build with Go and React" in a company
  blurb is not a demand, and it is not proof the job does not demand them.
- **No education stated** is `education: None`.

The filters in `search.py` then refuse to treat any of these as satisfied —
a posting that does not state pay does not pass "at least $150k".

## Where required and preferred come from

The heading a line sits under and the line's own wording, through
`experience.heading_kind` and `experience.line_demand` — the same reading the
years-of-experience filter uses, so the two cannot disagree about which list
is the nice-to-have one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from packages.matching.experience import Demand, heading_kind, line_demand
from packages.matching.skill_vocab import BY_KEY, find_skills

#: Bump when extraction changes, so the backfill knows which rows to redo.
EXTRACTOR_VERSION = 1

#: Evidence is a quote for a person to check, not a copy of the posting.
MAX_QUOTE = 240


class Requirement(StrEnum):
    REQUIRED = "required"
    PREFERRED = "preferred"
    #: Named in the posting, under nothing that says whether it is asked for.
    UNCLASSIFIED = "unclassified"


_FROM_DEMAND = {
    Demand.MANDATORY: Requirement.REQUIRED,
    Demand.PREFERRED: Requirement.PREFERRED,
    Demand.AMBIGUOUS: Requirement.UNCLASSIFIED,
}

#: Stronger first: a skill required in one line and preferred in another is required.
_STRENGTH = {Requirement.REQUIRED: 0, Requirement.PREFERRED: 1, Requirement.UNCLASSIFIED: 2}

EDUCATION_LEVELS = ("high_school", "associate", "bachelor", "master", "phd")

PERIODS = ("hour", "day", "week", "month", "year")


def _quote(line: str) -> str:
    text = " ".join(line.split())
    return text if len(text) <= MAX_QUOTE else text[: MAX_QUOTE - 1] + "…"


# --------------------------------------------------------------------------
# Compensation
# --------------------------------------------------------------------------

_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR", "₹": "INR"}
_CODES = ("USD", "CAD", "AUD", "NZD", "EUR", "GBP", "INR", "SGD", "CHF")
_CODE_RE = re.compile(rf"\b({'|'.join(_CODES)})\b")

_AMOUNT = r"(?P<{name}>\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?(?P<{name}k>[kK]\b)?"
_MONEY_RE = re.compile(
    r"(?P<sym>[$£€₹])\s?"
    + _AMOUNT.format(name="low")
    + r"(?:\s*(?:-|–|—|to)\s*(?P<sym2>[$£€₹])?\s?"
    + _AMOUNT.format(name="high")
    + r")?"
)

_PERIOD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("hour", re.compile(r"per\s+hour|/\s?h(?:ou)?r\b|\bhourly\b|an\s+hour", re.I)),
    ("day", re.compile(r"per\s+day|/\s?day\b|\bdaily\s+rate\b", re.I)),
    ("week", re.compile(r"per\s+week|/\s?w(?:ee)?k\b|\bweekly\b", re.I)),
    ("month", re.compile(r"per\s+month|/\s?mo(?:nth)?\b|\bmonthly\b", re.I)),
    (
        "year",
        re.compile(
            r"per\s+(?:year|annum)|/\s?y(?:ea)?r\b|\bannual(?:ly|ized)?\b|\byearly\b|a\s+year|p\.a\.",
            re.I,
        ),
    ),
)

#: A dollar figure that is not pay.
_NOT_PAY = re.compile(
    r"\b(?:raised|funding|funded|valuation|revenue|series\s+[a-f]|investors?|arr|"
    r"million|billion|stipend\s+for\s+equipment)\b|\d\s?[MB]\b|401\s?\(k\)",
    re.I,
)
_PAY_CONTEXT = re.compile(
    r"\b(?:salary|compensation|pay|base|wage|rate|ote|earnings|range)\b", re.I
)


@dataclass(frozen=True)
class Compensation:
    minimum: float
    maximum: float
    currency: str | None
    #: `None` when the posting did not say. Never assumed.
    period: str | None
    quote: str

    def as_dict(self) -> dict[str, object]:
        return {
            "minimum": self.minimum,
            "maximum": self.maximum,
            "currency": self.currency,
            "period": self.period,
            "quote": self.quote,
        }


def _number(raw: str, thousands: bool) -> float:
    value = float(raw.replace(",", ""))
    return value * 1000 if thousands else value


def _period_near(window: str) -> str | None:
    for period, pattern in _PERIOD_PATTERNS:
        if pattern.search(window):
            return period
    return None


def read_compensation(text: str | None) -> Compensation | None:
    """The first stated pay figure or range, with its currency and period."""
    if not text:
        return None
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if not line:
            continue
        for match in _MONEY_RE.finditer(line):
            around = line[max(0, match.start() - 60) : match.end() + 60]
            if _NOT_PAY.search(line[match.start() : match.end() + 25]) or _NOT_PAY.search(
                line[max(0, match.start() - 30) : match.start()]
            ):
                continue
            low = _number(match.group("low"), bool(match.group("lowk")))
            has_high = match.group("high") is not None
            high = _number(match.group("high"), bool(match.group("highk"))) if has_high else low
            # "$120k - 160k": the suffix was written once for both ends.
            if has_high and bool(match.group("lowk")) != bool(match.group("highk")) and high < low:
                high *= 1000
            previous = lines[index - 1] if index > 0 else ""
            period = _period_near(around) or _period_near(previous)
            context = _PAY_CONTEXT.search(line) or _PAY_CONTEXT.search(previous)
            # A lone figure with neither a period nor pay wording is too often
            # a price, a stipend or a statistic to be read as a salary.
            if not has_high and period is None and not context:
                continue
            if not context and period is None:
                continue
            code = _CODE_RE.search(around)
            currency = code.group(1) if code else _SYMBOLS.get(match.group("sym"))
            low, high = min(low, high), max(low, high)
            return Compensation(
                minimum=low, maximum=high, currency=currency, period=period, quote=_quote(line)
            )
    return None


# --------------------------------------------------------------------------
# Education
# --------------------------------------------------------------------------

_EDUCATION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("phd", re.compile(r"\b(?:ph\.?\s?d\.?|doctorate|doctoral\s+degree)", re.I)),
    (
        "master",
        re.compile(
            r"\b(?:master['’]?s|masters|m\.?s\.?(?=\s*(?:/|or|in|degree))|msc|m\.sc\.?|mba|m\.eng)\b",
            re.I,
        ),
    ),
    (
        "bachelor",
        re.compile(
            r"\b(?:bachelor['’]?s|bachelors|b\.?s\.?(?=\s*(?:/|or|in|degree))|"
            r"b\.?a\.?(?=\s*(?:/|or|in|degree))|bsc|b\.sc\.?|undergraduate\s+degree|"
            r"(?:4|four)[- ]year\s+degree)",
            re.I,
        ),
    ),
    ("associate", re.compile(r"\bassociate['’]?s?\s+degree", re.I)),
    ("high_school", re.compile(r"\bhigh\s+school\s+diploma|\bGED\b", re.I)),
)
_EQUIVALENT = re.compile(
    r"\bor\s+equivalent|equivalent\s+(?:practical\s+|work\s+|professional\s+)?experience"
    r"|or\s+related\s+experience|in\s+lieu\s+of",
    re.I,
)


@dataclass(frozen=True)
class Education:
    #: The lowest level the posting accepts, from `EDUCATION_LEVELS`.
    level: str
    requirement: Requirement
    #: "or equivalent experience" — the degree is one route, not the only one.
    equivalent_experience: bool
    quote: str

    def as_dict(self) -> dict[str, object]:
        return {
            "level": self.level,
            "requirement": self.requirement.value,
            "equivalent_experience": self.equivalent_experience,
            "quote": self.quote,
        }


# --------------------------------------------------------------------------
# The whole reading
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillMention:
    skill: str
    label: str
    requirement: Requirement
    quote: str

    def as_dict(self) -> dict[str, str]:
        return {"skill": self.skill, "label": self.label, "quote": self.quote}


@dataclass(frozen=True)
class PostingRequirements:
    compensation: Compensation | None = None
    skills: tuple[SkillMention, ...] = field(default_factory=tuple)
    education: Education | None = None

    def skills_by(self, requirement: Requirement) -> list[SkillMention]:
        return [mention for mention in self.skills if mention.requirement is requirement]

    @property
    def unknown(self) -> list[str]:
        """What the posting did not state, named so a screen can say so."""
        missing = []
        if self.compensation is None:
            missing.append("compensation")
        elif self.compensation.period is None:
            missing.append("pay_period")
        if not self.skills:
            missing.append("skills")
        if self.education is None:
            missing.append("education")
        return missing

    def as_json(self) -> dict[str, object]:
        return {
            "version": EXTRACTOR_VERSION,
            "compensation": self.compensation.as_dict() if self.compensation else None,
            "skills": {
                requirement.value: [m.as_dict() for m in self.skills_by(requirement)]
                for requirement in Requirement
            },
            "education": self.education.as_dict() if self.education else None,
            "unknown": self.unknown,
        }


#: A list item. `experience.heading_kind` never had to rule these out — a years
#: line carries digits, which a heading may not — but "- Kubernetes" is short
#: and punctuation-free, and read as a heading it was skipped *and* reset the
#: required/preferred context for every line after it.
_BULLET_START = re.compile(r"^\s*(?:[-–—*•·▪]\s+|\d+[.)]\s+)")


def _is_content(line: str) -> bool:
    """Whether a short line is an item rather than a heading."""
    if _BULLET_START.match(line):
        return True
    # A recognized requirements heading stays a heading however it is written.
    kind = heading_kind(line)
    if kind in (Demand.MANDATORY, Demand.PREFERRED):
        return False
    # "Kubernetes" or "PhD in Machine Learning" on a line of its own names a
    # thing; an unrecognized heading does not.
    return bool(find_skills(line)) or any(
        pattern.search(line) for _level, pattern in _EDUCATION_PATTERNS
    )


def extract(text: str | None) -> PostingRequirements:
    """Read pay, skills and education from a posting's text. Never guesses."""
    if not text:
        return PostingRequirements()

    heading = Demand.AMBIGUOUS
    best: dict[str, SkillMention] = {}
    education: list[Education] = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        kind = None if _is_content(line) else heading_kind(line)
        if kind is not None:
            heading = kind
            continue
        requirement = _FROM_DEMAND[line_demand(line, heading)]

        for skill in find_skills(line):
            current = best.get(skill.key)
            if current is None or _STRENGTH[requirement] < _STRENGTH[current.requirement]:
                best[skill.key] = SkillMention(skill.key, skill.label, requirement, _quote(line))

        levels = [level for level, pattern in _EDUCATION_PATTERNS if pattern.search(line)]
        if levels:
            # "BS or MS" accepts the lower one.
            lowest = min(levels, key=EDUCATION_LEVELS.index)
            education.append(
                Education(lowest, requirement, bool(_EQUIVALENT.search(line)), _quote(line))
            )

    ordered = tuple(
        sorted(best.values(), key=lambda m: (_STRENGTH[m.requirement], list(BY_KEY).index(m.skill)))
    )
    return PostingRequirements(
        compensation=read_compensation(text),
        skills=ordered,
        education=_strongest_education(education),
    )


def _strongest_education(found: list[Education]) -> Education | None:
    """The firmest statement wins; among equals, the lowest level accepted."""
    if not found:
        return None
    return min(
        found,
        key=lambda e: (_STRENGTH[e.requirement], EDUCATION_LEVELS.index(e.level)),
    )


__all__ = [
    "EDUCATION_LEVELS",
    "EXTRACTOR_VERSION",
    "PERIODS",
    "Compensation",
    "Education",
    "PostingRequirements",
    "Requirement",
    "SkillMention",
    "extract",
    "read_compensation",
]
