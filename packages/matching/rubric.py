"""A breakdown a person can argue with, instead of one number.

`Match.score` is a weighted cosine — 0.35 title, 0.65 body. It ranks well and
explains nothing. Told a posting scored 0.61, the owner cannot tell whether
the skills matched and the seniority was wrong, or the reverse, and so cannot
tell whether to apply.

career-ops (MIT) answers this with blocks A–F scored 1–5 apiece. Most of their
dimensions need an LLM and web research — compensation reliability, market
context, a customization plan. Ours are the ones already computed elsewhere in
this package, re-presented as named dimensions with a finding attached.

## This does not change the ranking

`Match.score` stays the cosine. Two reasons, and the second is the real one:

- The cosine is what `tests/test_matching.py` validates against hand-labeled
  postings, and what `Profile.min_match_score` has always compared against.
  Swapping the ranking function silently would invalidate both.
- A rubric built from signals the cosine already contains is not independent
  evidence. Ranking by it would look like a second opinion while being the
  same opinion in a different coat.

So this explains the ranking rather than producing it. If the rubric should
one day *become* the score, that is a deliberate change with a re-run of the
hand-labeled set beside it — not a side effect of adding an explanation.

## The scale

1 to 5, where 3 means "no signal either way" rather than "average". A posting
that does not state its seniority is not a poor seniority match; it is an
unknown one, and rounding unknowns down would penalise terse postings for
being terse.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from packages.core.models import Posting, Profile
from packages.matching.experience import read_posting as read_experience
from packages.matching.filters import SENIORITY_LEVELS, detect_seniority
from packages.matching.salary import Comparison, compare
from packages.matching.score import ScoredPosting

#: No signal. Distinct from a bad signal, which is 1 or 2.
NEUTRAL = 3.0


@dataclass(frozen=True)
class Dimension:
    """One scored axis, and why it scored that."""

    name: str
    score: float
    weight: float
    finding: str

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "score": round(self.score, 2),
            "weight": self.weight,
            "finding": self.finding,
        }


@dataclass
class Rubric:
    dimensions: list[Dimension] = field(default_factory=list)

    @property
    def overall(self) -> float:
        """Weighted mean on the 1–5 scale."""
        total_weight = sum(d.weight for d in self.dimensions)
        if not total_weight:
            return NEUTRAL
        return sum(d.score * d.weight for d in self.dimensions) / total_weight

    @property
    def weakest(self) -> Dimension | None:
        """The dimension dragging this down — the one worth reading first."""
        scored = [d for d in self.dimensions if d.weight]
        return min(scored, key=lambda d: d.score) if scored else None

    def as_dict(self) -> dict[str, object]:
        weakest = self.weakest
        return {
            "overall": round(self.overall, 2),
            "dimensions": [d.as_dict() for d in self.dimensions],
            "weakest": weakest.name if weakest else None,
        }


def _scale(value: float, *, low: float, high: float) -> float:
    """Map a 0–1 signal onto 1–5, clamped."""
    if high <= low:
        return NEUTRAL
    fraction = (value - low) / (high - low)
    return max(1.0, min(5.0, 1.0 + 4.0 * fraction))


def _role_fit(scored: ScoredPosting) -> Dimension:
    """How close the title is to what the profile describes."""
    # Cosine over short strings rarely exceeds ~0.6 even for a good match, so
    # the band is set to what titles actually score rather than to 0–1.
    value = _scale(scored.title_similarity, low=0.05, high=0.55)
    return Dimension(
        name="role_fit",
        score=value,
        weight=0.30,
        finding=f"title similarity {scored.title_similarity:.2f}",
    )


def _skills_coverage(scored: ScoredPosting) -> Dimension:
    """Share of the terms this posting emphasizes that the profile evidences."""
    matched = len(scored.matched_terms)
    missing = len(scored.missing_terms)
    total = matched + missing

    if not total:
        return Dimension("skills_coverage", NEUTRAL, 0.0, "no comparable terms in the posting")

    coverage = matched / total
    return Dimension(
        name="skills_coverage",
        score=_scale(coverage, low=0.1, high=0.8),
        weight=0.35,
        finding=(
            f"{matched} of {total} emphasized terms evidenced"
            + (f"; missing {', '.join(scored.missing_terms[:4])}" if missing else "")
        ),
    )


def _seniority(posting: Posting, target: str | None) -> Dimension:
    """Whether the advertised level is near the one being targeted."""
    advertised = detect_seniority(" ".join(filter(None, [posting.title, posting.location])))

    if advertised is None or not target:
        return Dimension(
            "seniority",
            NEUTRAL,
            0.0,
            "posting does not state a level" if advertised is None else "no target level set",
        )

    order = [level for level, _ in SENIORITY_LEVELS]
    try:
        distance = abs(order.index(advertised) - order.index(target))
    except ValueError:
        return Dimension("seniority", NEUTRAL, 0.0, f"unknown level {advertised!r}")

    # Exact is 5, one rung either way is 3.5, two is 2, beyond that is 1.
    score = {0: 5.0, 1: 3.5, 2: 2.0}.get(distance, 1.0)
    return Dimension(
        name="seniority",
        score=score,
        weight=0.20,
        finding=(
            f"advertised {advertised}, targeting {target}"
            + ("" if distance == 0 else f" ({distance} rung{'s' if distance > 1 else ''} apart)")
        ),
    )


def _eligibility(scored: ScoredPosting) -> Dimension:
    """The hard filters, restated. Excluded is 1 and means it, not 'low'."""
    if scored.excluded_by:
        return Dimension(
            name="eligibility",
            score=1.0,
            weight=0.15,
            finding="; ".join(scored.excluded_by),
        )
    return Dimension("eligibility", 5.0, 0.15, "passes location, sponsorship and clearance")


def _salary(posting: Posting, profile: Profile) -> Dimension:
    """Where the advertised pay sits against the expectation on the profile.

    Zero weight whenever either side is silent, which is most of the time.
    A posting that declines to state a range is not a worse posting, and
    scoring it as one would penalise employers for saying less.
    """
    finding = compare(posting.description_raw or "", profile.salary_expectation)

    if finding.comparison is Comparison.UNKNOWN:
        return Dimension("salary", NEUTRAL, 0.0, finding.finding)

    score = {Comparison.ABOVE: 5.0, Comparison.WITHIN: 4.5, Comparison.BELOW: 1.5}[
        finding.comparison
    ]
    return Dimension("salary", score, 0.15, finding.finding)


def _experience(posting: Posting, max_years: int | None) -> Dimension:
    """The years the posting demands, against the owner's bound.

    This is where a *preferred* demand has its effect. `filters.experience_ok`
    refuses to exclude on one — the posting said the requirement was optional,
    and overruling that would hide a job the employer was inviting — so the
    cost of being under it belongs here, where it lowers a dimension the owner
    can read and argue with instead of removing the posting from the feed.

    Zero weight when the posting states nothing or the owner set no bound,
    matching `_salary`: a terse posting is not a worse posting.

    An ambiguous demand also carries zero weight and still reports its finding.
    `experience.py` refuses to treat a years line under no heading as evidence
    of how firm it is, and a dimension that scored one anyway would be deciding
    in the rubric what the filter declined to decide.
    """
    demanded = read_experience(posting.description_raw)
    if not demanded.stated:
        return Dimension("experience", NEUTRAL, 0.0, "posting does not state a years requirement")

    summary = demanded.summary() or ""
    if max_years is None:
        return Dimension("experience", NEUTRAL, 0.0, f"{summary}; no limit set")

    mandatory = demanded.mandatory_minimum
    if mandatory is not None:
        score = 5.0 if mandatory <= max_years else 1.0
        return Dimension("experience", score, 0.15, f"{summary}, limit {max_years}")

    preferred = demanded.preferred_minimum
    if preferred is not None:
        score = 5.0 if preferred <= max_years else 2.5
        return Dimension("experience", score, 0.15, f"{summary}, limit {max_years}")

    return Dimension(
        "experience", NEUTRAL, 0.0, f"{summary}, stated as neither required nor preferred"
    )


def evaluate(
    posting: Posting,
    profile: Profile,
    scored: ScoredPosting,
    *,
    target_seniority: str | None = None,
    max_required_experience_years: int | None = None,
) -> Rubric:
    """Break a score down into dimensions the owner can act on.

    Every input is already computed by `score_posting`; nothing here fetches,
    infers, or asks a model.
    """
    return Rubric(
        dimensions=[
            _role_fit(scored),
            _skills_coverage(scored),
            _seniority(posting, target_seniority),
            _experience(
                posting,
                max_required_experience_years
                if max_required_experience_years is not None
                else profile.max_required_experience_years,
            ),
            _eligibility(scored),
            _salary(posting, profile),
        ]
    )
