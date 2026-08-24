"""Is this posting a real, open job? Kept apart from whether it fits.

The match score answers "does this suit me". It cannot answer "is this
real", and conflating the two puts a well-written ghost job at the top of
the feed precisely because it is well-written. So this produces a **tier and
a findings table, never a number**, and nothing here touches `Match.score`.

## Why this matters more here than in a tool that only drafts

Jobrunner fills real forms with a real person's phone number, address, and
work-authorization answers. A posting that exists to harvest that is a safety
problem, not a quality one. A tool that only prepares drafts for a human to
send has a person reading every posting before anything is disclosed; we do
not, which is exactly why the check has to be automatic.

## What this deliberately does not do

No LLM, no web research, no lookups. Every signal is computed from the
posting we already hold and its siblings in the database, because discovery
runs on a schedule over thousands of postings and anything per-posting and
online would not survive contact with that.

That rules out the strongest signals — layoff announcements, market context
for the role — and leaves the mechanical ones. It is a filter for the obvious
cases, not a judgement. `SUSPICIOUS` means look before you leap, never "this
is a scam", and the findings say what was observed rather than what it means.

Idea and the tier/findings structure from santifer/career-ops (MIT), whose
Block G is the fuller version of this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from packages.core.models import Posting
from packages.matching.embed import tokenize
from packages.matching.idf import DocumentFrequencies
from packages.matching.score import _BOILERPLATE, _proper_nouns
from packages.matching.topics import TopicModel, entropy


class Weight(StrEnum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    CONCERNING = "concerning"


class Tier(StrEnum):
    HIGH_CONFIDENCE = "high_confidence"
    CAUTION = "caution"
    SUSPICIOUS = "suspicious"


@dataclass(frozen=True)
class Signal:
    """One observation. States what was seen, not what it proves."""

    name: str
    weight: Weight
    finding: str


@dataclass
class Assessment:
    tier: Tier
    signals: list[Signal] = field(default_factory=list)
    #: Findings that are worth knowing but say nothing about legitimacy —
    #: contractor wording, a benefits/geography mismatch. A posting can be
    #: entirely real and still be one of these.
    advisories: list[Signal] = field(default_factory=list)

    @property
    def concerning(self) -> list[Signal]:
        return [s for s in self.signals if s.weight is Weight.CONCERNING]

    def as_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier.value,
            "signals": [
                {"name": s.name, "weight": s.weight.value, "finding": s.finding}
                for s in self.signals
            ],
            "advisories": [
                {"name": s.name, "weight": s.weight.value, "finding": s.finding}
                for s in self.advisories
            ],
        }


#: Past this a posting has been open long enough to be worth asking about.
#: Not evidence on its own — executive and government hiring runs long.
STALE_AFTER = timedelta(days=60)
VERY_STALE_AFTER = timedelta(days=120)

#: Below this share of distinct meaningful words, a description is repeating
#: itself rather than describing anything — filler padded to look substantial.
#:
#: Deliberately conservative. Measured on two hand-written fixtures a real
#: posting scored 0.82 and a filler one 0.67, and putting the line between
#: those two numbers would be tuning to samples this repo wrote — the exact
#: circularity noted in docs/REFERENCE.md §3.6. So the threshold sits well
#: below both and catches only egregious repetition. Recalibrate it against
#: real postings, with real postings.
MIN_SPECIFICITY = 0.55

#: A description shorter than this cannot state a scope. Counted *after*
#: stopword removal, so it is roughly half the raw word count.
MIN_DESCRIPTION_WORDS = 45

#: Wording that makes this a contract, not employment. Orthogonal to whether
#: the posting is real, and worth surfacing either way.
_CONTRACTOR_RE = re.compile(
    r"\b(1099|t4a|corp[- ]to[- ]corp|c2c|independent contractor|"
    r"consulting agreement|statement of work|invoice(?:s|d|ing)?\s+(?:us|monthly|the company))\b",
    re.I,
)

#: Country-specific benefits vocabulary, for comparing against the location.
_BENEFITS_BY_COUNTRY = {
    "us": re.compile(r"\b(401\s?\(?k\)?|hsa|fsa|aca)\b", re.I),
    "uk": re.compile(r"\b(pension scheme|nhs|national insurance)\b", re.I),
    "ca": re.compile(r"\b(rrsp|tfsa|group rrsp)\b", re.I),
}

_COUNTRY_HINTS = {
    "us": ("united states", "usa", " us ", "california", "new york", "texas", "remote - us"),
    "uk": ("united kingdom", "england", "london", "scotland"),
    "ca": ("canada", "toronto", "vancouver", "ontario", "québec", "quebec"),
    "in": ("india", "bengaluru", "bangalore", "hyderabad", "mumbai"),
}

#: A range this wide relative to its floor is not a pay range, it is a
#: placeholder. Heuristic, not a compliance check.
MAX_RANGE_RATIO = 1.5

_SALARY_RE = re.compile(
    r"[$£€]\s?(\d{2,3}(?:,\d{3})?)(?:\s?k)?\s*(?:-|–|to)\s*[$£€]?\s?(\d{2,3}(?:,\d{3})?)(?:\s?k)?",
    re.I,
)


def _country_of(location: str | None) -> str | None:
    text = f" {(location or '').lower()} "
    for country, hints in _COUNTRY_HINTS.items():
        if any(hint in text for hint in hints):
            return country
    return None


def specificity(text: str, frequencies: DocumentFrequencies | None = None) -> float:
    """Share of tokens that are distinctive rather than posting furniture.

    With `frequencies`, "furniture" is measured against the corpus instead of
    read off a hand list. That matters here more than in the gap report: the
    threshold below was calibrated on two fixtures written in this repo, which
    is exactly the circularity docs/REFERENCE.md §3.6 warns about. A corpus
    statistic does not have that problem.
    """
    tokens = tokenize(text)
    if not tokens:
        return 0.0
    proper = _proper_nouns(text)

    if frequencies is not None and frequencies.usable:
        distinctive = {
            token for token in tokens if token in proper or not frequencies.is_boilerplate(token)
        }
        return len(distinctive) / len(tokens)

    distinctive = {token for token in tokens if token in proper or token not in _BOILERPLATE}
    # Distinct distinctive terms against total length: a posting that repeats
    # "collaborative" twenty times does not become specific by doing so.
    return len(distinctive) / len(tokens)


def _freshness(posting: Posting, now: datetime) -> Signal:
    first_seen = posting.first_seen_at
    if first_seen is None:
        return Signal("freshness", Weight.NEUTRAL, "no first-seen date recorded")
    if first_seen.tzinfo is None:
        first_seen = first_seen.replace(tzinfo=UTC)

    age = now - first_seen
    days = age.days
    if age >= VERY_STALE_AFTER:
        return Signal("freshness", Weight.CONCERNING, f"open for {days} days")
    if age >= STALE_AFTER:
        return Signal("freshness", Weight.NEUTRAL, f"open for {days} days")
    return Signal("freshness", Weight.POSITIVE, f"first seen {days} days ago")


def _description_quality(
    posting: Posting, frequencies: DocumentFrequencies | None = None
) -> Signal:
    text = posting.description_raw or ""
    words = tokenize(text)

    if len(words) < MIN_DESCRIPTION_WORDS:
        return Signal(
            "description_quality",
            Weight.CONCERNING,
            f"description is {len(words)} words; too short to state a scope",
        )

    ratio = specificity(text, frequencies)
    if ratio < MIN_SPECIFICITY:
        return Signal(
            "description_quality",
            Weight.CONCERNING,
            f"{ratio:.0%} distinctive terms; reads as boilerplate",
        )
    return Signal("description_quality", Weight.POSITIVE, f"{ratio:.0%} distinctive terms")


def _reposting(posting: Posting, siblings: list[Posting]) -> Signal:
    """The same role reappearing under new ids — the classic ghost tell."""
    title = (posting.title or "").strip().lower()
    if not title:
        return Signal("reposting", Weight.NEUTRAL, "no title to compare")

    twins = [
        other
        for other in siblings
        if other.id != posting.id
        and other.company_id == posting.company_id
        and (other.title or "").strip().lower() == title
    ]
    if len(twins) >= 2:
        return Signal(
            "reposting",
            Weight.CONCERNING,
            f"{len(twins) + 1} postings for this title at this company",
        )
    if twins:
        return Signal("reposting", Weight.NEUTRAL, "one other posting for this title")
    return Signal("reposting", Weight.POSITIVE, "no duplicate postings for this title")


def _contractor_wording(posting: Posting) -> Signal | None:
    match = _CONTRACTOR_RE.search(posting.description_raw or "")
    if match is None:
        return None
    return Signal(
        "employment_classification",
        Weight.CONCERNING,
        f"posting uses contract wording: {match.group(0)!r}",
    )


def _benefits_geography(posting: Posting) -> Signal | None:
    country = _country_of(posting.location)
    if country is None:
        return None
    text = posting.description_raw or ""
    for other, pattern in _BENEFITS_BY_COUNTRY.items():
        if other == country:
            continue
        found = pattern.search(text)
        if found and not (_BENEFITS_BY_COUNTRY.get(country) or re.compile(r"$^")).search(text):
            return Signal(
                "benefits_geography",
                Weight.CONCERNING,
                f"location reads as {country.upper()} but benefits mention "
                f"{found.group(0)!r}, which is {other.upper()}",
            )
    return None


def _salary_range(posting: Posting) -> Signal | None:
    match = _SALARY_RE.search(posting.description_raw or "")
    if match is None:
        return None
    try:
        low = float(match.group(1).replace(",", ""))
        high = float(match.group(2).replace(",", ""))
    except ValueError:  # pragma: no cover - the regex constrains this
        return None
    if low <= 0 or high <= low:
        return None
    if (high - low) / low > MAX_RANGE_RATIO:
        return Signal(
            "salary_range",
            Weight.CONCERNING,
            f"advertised range spans {low:.0f}-{high:.0f}, wider than the role can mean",
        )
    return None


#: Above this share of the maximum entropy, a posting's topic mass is spread
#: widely enough that it is not about one job.
#:
#: Measured, not chosen. `make fit-topics -k 8 -n 200` over the real corpus
#: gives p10=0.72 p25=0.80 p50=0.86 p75=0.91 p90=0.94 p99=0.97. The first
#: draft of this constant was 0.85, which is *below the median* — it would
#: have flagged more than half of every sweep, and a warning that fires on the
#: majority is the one nobody reads. Set at the p99 tail instead.
#:
#: **Read `_topic_focus`'s docstring before trusting the number.** The
#: threshold is sound; the model underneath it is not yet.
MAX_TOPIC_ENTROPY = 0.97


def _topic_focus(posting: Posting, model: TopicModel) -> Signal:
    """How many subjects this posting is about at once.

    Deliberately independent of `specificity()`. That counts distinctive
    words; this asks whether they describe one job. A posting stuffed with
    jargon from four unrelated fields passes the first and fails this one,
    and that combination is the one worth catching.

    ## This is not yet a trustworthy signal

    Fitted over 200 real postings, the topics come out dominated by boilerplate
    and employer names — "this systems teams engineering", "benefits customers
    status about platform" — rather than by job families. Topics that do not
    separate roles cannot support a claim that a posting straddles several,
    however clean the entropy arithmetic on top of them is.

    Two plausible causes, neither investigated: `embed.py`'s stopword list is
    tuned for scoring rather than for topic modelling and lets company names
    and benefits language through, and the calibration run used 40 iterations
    where the sampler defaults to 200.

    It ships because it is inert — `assess()` omits the signal unless a caller
    fits and passes a model, and nothing in the codebase does. The machinery is
    tested and correct. What is missing is evidence the topics mean anything.
    """
    distribution = model.transform(posting.description_raw or "")
    spread = entropy(distribution)

    if spread > MAX_TOPIC_ENTROPY:
        return Signal(
            "topic_focus",
            Weight.CONCERNING,
            f"topic mass spread at {spread:.0%} of maximum; reads as several roles at once",
        )
    return Signal("topic_focus", Weight.POSITIVE, f"topic mass concentrated ({spread:.0%})")


def assess(
    posting: Posting,
    *,
    siblings: list[Posting] | None = None,
    now: datetime | None = None,
    frequencies: DocumentFrequencies | None = None,
    topics: TopicModel | None = None,
) -> Assessment:
    """Tier this posting's legitimacy. Never returns or affects a score.

    `topics` is optional on purpose. LDA has to be fit over a corpus before it
    can say anything about one document, and this function is called per
    posting across thousands of them. Pass a model fitted by
    `scripts.fit_topics` to gain the signal; omit it and the cost is unchanged.
    """
    moment = now or datetime.now(UTC)

    signals = [
        _freshness(posting, moment),
        _description_quality(posting, frequencies),
        _reposting(posting, siblings or []),
    ]
    if topics is not None:
        signals.append(_topic_focus(posting, topics))

    advisories = [
        signal
        for signal in (
            _contractor_wording(posting),
            _benefits_geography(posting),
            _salary_range(posting),
        )
        if signal is not None
    ]

    concerning = sum(1 for signal in signals if signal.weight is Weight.CONCERNING)

    # Two independent concerns is the bar for SUSPICIOUS. One is common and
    # innocent — a genuinely old posting, a genuinely terse one — and calling
    # those suspicious would make the tier noise, which is how a warning stops
    # being read.
    if concerning >= 2:
        tier = Tier.SUSPICIOUS
    elif concerning == 1 or any(s.weight is Weight.NEUTRAL for s in signals):
        tier = Tier.CAUTION
    else:
        tier = Tier.HIGH_CONFIDENCE

    return Assessment(tier=tier, signals=signals, advisories=advisories)
