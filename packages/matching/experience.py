"""How many years of experience a posting demands, and how firmly.

Spec §13 and `docs/BACKLOG.md` P7. `Posting` records no experience
requirement at all, so a role asking for ten years is ranked against a new
graduate's résumé by cosine similarity alone — and a cosine cannot refuse a
seniority demand, which is the same finding `filters.seniority_ok` already has
a number attached to.

**The split is the point, not the number.** A years line under "Minimum
qualifications" and the same line under "Nice to have" are different facts, and
collapsing them would make this filter drop postings that had already said the
requirement was optional. So every demand carries a `Demand`, and only
`MANDATORY` is allowed to exclude. That mirrors `eligibility.py`: silence is
not an answer, ambiguity is not an answer, and a hard filter needs evidence.

Measured on the twelve real crawled postings in `tests/fixtures/golden/`,
which is where the rules below come from:

    5+ years of leadership experience directly managing ...      -> 5
    8–12+ years of experience, with a track record of ...        -> 8   (range)
    typically have 10-15 years of experience in a customer ...   -> 10  (range)
    3+ years’ experience within a large public accounting firm   -> 3   (curly)
    2+ years of investment banking at a top-tier firm            -> 2   (no "experience")
    Palantir Year / Come join us for a year                      -> none

Two of those shaped the acceptance rule. `3+ years field experience` never says
"years of", and `2+ years of investment banking` never says "experience" — so
neither a connector alone nor an experience noun alone is sufficient. A line is
read as a demand when an experience noun follows nearby **or** the line opens
with the years phrase, which is how a qualification bullet is written and is
not how prose mentions a duration ("able to commit to 2 years in the role").

**The minimum across mandatory demands is the binding one, not the maximum.**
A posting listing "8-10 years in Strategic Finance" and "2+ years of
investment banking" offers two routes in, and this module cannot tell a
conjunction from an alternative. Taking the smallest keeps the posting and
shows the owner every demand; taking the largest would hide a job they may be
able to hold. Keeping and explaining is what `locality.py` does with an
unplaced city, for the same reason: a hard filter that guesses wrong in the
excluding direction is invisible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from packages.matching.eligibility import Evidence

#: How far after a years token an experience noun still counts as attached to
#: it. One clause, roughly — long enough for "3+ years field experience of
#: quota-carrying experience", short enough that the next bullet's wording
#: cannot rescue a duration that is not a requirement.
_ATTACHMENT_CHARS = 80


class Demand(StrEnum):
    """How firmly the posting asks for a years requirement."""

    #: Stated as required, or listed under a requirements heading.
    MANDATORY = "mandatory"
    #: Stated as preferred, or listed under a nice-to-have heading.
    PREFERRED = "preferred"
    #: A demand with no indication either way. Never excludes.
    AMBIGUOUS = "ambiguous"


#: `8–12+ years`, `8-10 years`, `5+ yrs`. The first number is captured because
#: a range's lower bound is the requirement — reading `8–12+ years` as twelve
#: would exclude a posting that says eight.
_YEARS = re.compile(
    r"(?P<years>\d{1,2})\s*(?:[-–—]\s*\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b",
    re.I,
)

#: A noun that makes a duration a statement about the applicant's background.
_EXPERIENCE_NOUN = re.compile(r"\b(?:experience|expertise|background)\b", re.I)

#: Read immediately after the years token. `years of`, `years in`, `years’`.
_CONNECTOR = re.compile(r"\s*(?:of|in|with|as)\b|\s*['’]s?\b", re.I)

#: Durations that are not requirements. Every one of these reaches the years
#: pattern and none of them is a demand on the applicant.
_NOT_A_DEMAND = re.compile(
    r"\b(?:"
    r"\d{1,2}\s*(?:[-–—]\s*\d{1,2})?\s*\+?\s*years?\s+ago"
    r"|(?:past|last|next|coming|first)\s+\d{1,2}\s*\+?\s*years?"
    r"|within\s+\d{1,2}\s*\+?\s*years?"
    r"|every\s+\d{1,2}\s*\+?\s*years?"
    r"|\d{1,2}\s*[-–—]?\s*year\s+(?:degree|program|programme|contract|course|rotation|visa)"
    r"|commit(?:ment)?\s+to\s+\d{1,2}\s*\+?\s*years?"
    r")",
    re.I,
)

#: Headings are read from an allowlist of *openers*, and a line that is
#: heading-shaped but on neither list resets the context to `AMBIGUOUS`. That
#: reset is load-bearing: without it a years line under "Benefits" or "Pay
#: Range Transparency" inherits whatever requirements heading came before it,
#: which on a real posting is several screens up.
#:
#: `tailor/ats.py::_REQUIREMENT_HEADING` is deliberately not reused. It answers
#: a different question — where the requirements *start*, so it matches
#: "What You'll Do" on purpose — and it cannot say whether a heading is asking
#: or wishing, which is the only thing this module wants from one.
_MANDATORY_HEADING = re.compile(
    r"^(?:"
    r"requirements?"
    r"|qualifications?"
    r"|(?:minimum|basic|required|core|must[- ]have)\s+(?:qualifications?|requirements?|skills?)"
    r"|who\s+you\s+are"
    r"|your\s+background"
    r"|about\s+you"
    r"|what\s+we(?:['’]re|\s+are)?\s+look(?:ing)?\s+for"
    r"|what\s+we\s+(?:require|need)"
    r"|what\s+you(?:['’]ll|\s+will)?\s+(?:need|bring)"
    r"|what\s+you\s+bring"
    r"|skills?\s+(?:and|&)\s+experience"
    r"|experience\s+(?:and|&)\s+skills?"
    r")\b",
    re.I,
)

#: Tested *before* the mandatory list, because "Preferred qualifications"
#: matches both and the qualifier is the half that decides.
#:
#: `things we love` is MongoDB's nice-to-have heading, `bonus points` is
#: Datadog's, and Palantir pairs `what we value` with `what we require` — all
#: taken from the crawled corpus rather than guessed, which is the §15 lesson
#: about a pattern and the fixture that exercises it being written together.
#:
#: `what we value` is the one worth noticing: it reads like a statement of
#: principles and is in fact the nice-to-have list, carrying "CPA or ACCA/ACA
#: (preferred but not required)" on the posting it came from.
_PREFERRED_HEADING = re.compile(
    r"^(?:"
    r"(?:preferred|desired|desirable|additional|ideal|bonus)"
    r"(?:\s+(?:qualifications?|requirements?|skills?|experience))?"
    r"|nice\s+to\s+have"
    r"|good\s+to\s+have"
    r"|bonus\s+points"
    r"|pluses?"
    r"|even\s+better"
    r"|extra\s+credit"
    r"|things\s+we\s+love"
    r"|what\s+we\s+value"
    r"|what\s+would\s+set\s+you\s+apart"
    r")\b",
    re.I,
)

#: The most words a heading may have. "Your background looks something like
#: this" is six, and is a real heading from the corpus.
_MAX_HEADING_WORDS = 8

#: Characters that appear in a posting's *content* and not in its headings —
#: the same trick `tailor/ats.py` uses on résumé sections, and the reason
#: "Observability: Datadog, Splunk, AppD, New Relic or Dynatrace" is read as a
#: bullet rather than as a heading that would reset the context.
_NOT_IN_A_HEADING = ",;()[]0123456789•·▪|"


#: Wording inside the line itself, which outranks the heading it sits under:
#: "8+ years preferred" in a requirements list is preferred, and "minimum 5
#: years" under a nice-to-have list is not.
_LINE_PREFERRED = re.compile(
    r"\b(?:preferred|preferably|ideally|a\s+plus|nice\s+to\s+have|bonus|desirable)\b", re.I
)
_LINE_MANDATORY = re.compile(
    r"\b(?:required|must\s+have|minimum|at\s+least|no\s+less\s+than)\b", re.I
)

#: Bullet glyphs and list punctuation stripped before asking whether the line
#: *opens* with its years phrase.
_BULLET = re.compile(r"^\s*(?:[-–—*•·▪o]\s+|\d+[.)]\s+)?")


@dataclass(frozen=True)
class YearsDemand:
    """One years requirement, with the sentence it was read from."""

    years: int
    demand: Demand
    evidence: Evidence

    def describe(self) -> str:
        if self.demand is Demand.MANDATORY:
            return f"requires {self.years}+ years"
        if self.demand is Demand.PREFERRED:
            return f"prefers {self.years}+ years"
        return f"mentions {self.years}+ years"


@dataclass(frozen=True)
class PostingExperience:
    """What one posting demands in years — a property of the posting.

    Whether a demand excludes *this* applicant is a filter's job and needs the
    owner's own bound, so the same reading can be shown on any feed.
    """

    demands: tuple[YearsDemand, ...] = field(default_factory=tuple)

    @property
    def stated(self) -> bool:
        return bool(self.demands)

    @property
    def mandatory_minimum(self) -> int | None:
        """The smallest number of years the posting insists on, if it does.

        The smallest rather than the largest — see the module docstring. None
        when nothing mandatory was found, which includes a posting whose only
        years line is preferred.
        """
        years = [d.years for d in self.demands if d.demand is Demand.MANDATORY]
        return min(years) if years else None

    @property
    def preferred_minimum(self) -> int | None:
        years = [d.years for d in self.demands if d.demand is Demand.PREFERRED]
        return min(years) if years else None

    def summary(self) -> str | None:
        """One line for a card, or None when the posting said nothing.

        None rather than a phrase like "no experience requirement": most
        postings simply do not state one, and rendering that absence as a
        finding would make a silence look like a welcome.
        """
        if not self.demands:
            return None
        return "; ".join(
            dict.fromkeys(d.describe() for d in sorted(self.demands, key=lambda d: d.years))
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "stated": self.stated,
            "mandatory_minimum": self.mandatory_minimum,
            "preferred_minimum": self.preferred_minimum,
            "summary": self.summary(),
            "demands": [
                {
                    "years": d.years,
                    "demand": d.demand.value,
                    "quote": d.evidence.quote,
                }
                for d in self.demands
            ],
        }


def _is_demand(line: str, match: re.Match[str]) -> bool:
    """Whether this duration is a requirement on the applicant.

    Two acceptances, because neither covers the corpus alone. An experience
    noun nearby catches `3+ years field experience`, which never says "of";
    opening the line catches `2+ years of investment banking`, which never
    says "experience". Prose mentioning a duration does neither.
    """
    if _NOT_A_DEMAND.search(line):
        return False

    after = line[match.end() : match.end() + _ATTACHMENT_CHARS]
    if _EXPERIENCE_NOUN.search(after):
        return True
    # The years phrase opens the line: how a qualification bullet is written.
    return _BULLET.sub("", line).lower().startswith(line[match.start() : match.end()].lower())


def _heading_kind(line: str) -> Demand | None:
    """`MANDATORY`, `PREFERRED`, `AMBIGUOUS` for another heading, None for a
    line that is not a heading at all.

    The three-way answer is why this is not a boolean: a heading nobody
    recognises still has to clear the previous one.
    """
    stripped = line.strip().rstrip(":").strip()
    if not stripped or len(stripped.split()) > _MAX_HEADING_WORDS:
        return None
    if stripped.endswith((".", ",", ";", "!", "?")):
        return None
    if any(c in _NOT_IN_A_HEADING for c in stripped):
        return None
    if _PREFERRED_HEADING.match(stripped):
        return Demand.PREFERRED
    if _MANDATORY_HEADING.match(stripped):
        return Demand.MANDATORY
    return Demand.AMBIGUOUS


def _demand_of(line: str, heading: Demand) -> Demand:
    """The line's own wording first, then the heading it sits under."""
    if _LINE_PREFERRED.search(line):
        return Demand.PREFERRED
    if _LINE_MANDATORY.search(line):
        return Demand.MANDATORY
    return heading


def read_posting(text: str | None) -> PostingExperience:
    """Read every years demand in a posting. Never guesses a number.

    Headings are tracked down the document rather than the section being cut
    out first, because `tailor/ats.py::_requirements_text` answers a different
    question — where the requirements *start* — and a preferred sub-list lives
    inside that same section.
    """
    if not text:
        return PostingExperience()

    heading = Demand.AMBIGUOUS
    demands: list[YearsDemand] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        kind = _heading_kind(line)
        if kind is not None:
            heading = kind
            continue

        for match in _YEARS.finditer(line):
            if not _is_demand(line, match):
                continue
            demands.append(
                YearsDemand(
                    years=int(match.group("years")),
                    demand=_demand_of(line, heading),
                    evidence=Evidence.of("experience", line),
                )
            )

    return PostingExperience(demands=tuple(demands))


def heading_kind(line: str) -> Demand | None:
    """Public form of the heading classifier, for readers of other requirements.

    `requirements.py` reads skills and education under the same headings this
    module reads years under. Two copies of "is this the nice-to-have list"
    would disagree on the next heading someone adds to one of them.
    """
    return _heading_kind(line)


def line_demand(line: str, heading: Demand) -> Demand:
    """Public form of `_demand_of`: the line's own wording, then its heading."""
    return _demand_of(line, heading)


__all__ = [
    "Demand",
    "PostingExperience",
    "YearsDemand",
    "heading_kind",
    "line_demand",
    "read_posting",
]
