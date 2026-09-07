"""Is this application ready to send, and what is stopping it?

The spec puts a composite in front of every approval (§25, §53) and asks that a
raw float be given a legible band (§44). Both exist because `/review` had grown
four independent numbers — the match rubric, the ATS parse and keyword scores,
the recruiter levels — and no answer to the only question the owner is actually
asking at that screen, which is *should this go*.

## The gate is not the score, and that is the whole point

§53 asks for a quality gate, not a threshold on an average. The two come apart
precisely where it matters: an application can score well on every measurable
axis and still be unsendable because a required question has no answer, or
because the honest answer to a knock-out disqualifies. Averaging those in would
let a strong résumé outvote a missing work-authorization answer, which is the
§2.4 failure — guessing at an unanswerable question — arriving by arithmetic
instead of by a model.

So `blockers` is a separate list, and `is_ready` is false whenever it is
non-empty **regardless of `score`**. A blocker is a fact about whether the
application can be completed; the score is an opinion about how well it would
land. `test_a_blocker_beats_a_perfect_score` holds that line.

## A component that could not be measured is absent, not zero

The same reasoning `AtsReport.scored_against_posting` and
`RecruiterReport.scored_against_posting` already carry. With no posting text
the keyword and scan levels are not meaningful, and folding them in as 0.0
would report a well-prepared application as unready because the *crawler* fell
short. Folding them in as 1.0 is worse — it flatters. An unmeasurable component
gets zero weight and says so in its finding, so the screen shows a named gap
rather than a number that quietly moved.

## Below three measured components this refuses to report a score

`docs/BACKLOG.md` names the trap this module was most likely to fall into: "a
readiness score over two inputs is a rename, not a score". A weighted mean of
the ATS parse score and itself is not a composite, and presenting it as one
would put a confident number in front of an approval on the strength of a
single measurement. `score` returns None below `MIN_COMPONENTS`, `tier` returns
None with it, and the screen says what is missing instead.

That is also why `is_ready` requires a score: an application nobody could
measure is not ready, it is unassessed, and those must not look alike at the
moment of sending.

## It never overrides the owner

`is_ready` gates the *unattended* path — the same standing as the knock-out
check in `apps/worker/apply_job.py`. §2.3 makes the owner's explicit approval
the authorization, so an application they have looked at and approved submits
whether or not this module liked it. This is advice at the moment of approval,
and a gate only when nobody is there to read the advice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "Blocker",
    "Component",
    "MIN_COMPONENTS",
    "READY_FLOOR",
    "Readiness",
    "Tier",
    "assess",
]

#: Fewer measured components than this and no score is reported. See the module
#: docstring — two inputs is a rename, not a composite.
MIN_COMPONENTS = 3

#: The unattended path needs this much on top of an empty `blockers` list.
#: Deliberately not high: the blockers carry the hard reasons, and a second
#: strict threshold here would park applications for being merely average,
#: which the owner can already express with `Profile.min_match_score`.
READY_FLOOR = 0.60


class Tier(StrEnum):
    """§44's bands, so a raw float reads as something.

    The boundaries are the spec's at the top (95–100, 90–94) and chosen below
    that. They are deliberately harsh in the middle for the reason
    `recruiter.Shortlist` is: most real applications are ordinary, and a curve
    that calls them "strong" tells the owner nothing they can act on.
    """

    EXCEPTIONAL = "exceptional"
    VERY_STRONG = "very strong"
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"


#: Descending, so the first match wins. Kept beside `Tier` because a band whose
#: floor lives somewhere else drifts against the name.
_BANDS: tuple[tuple[float, Tier], ...] = (
    (0.95, Tier.EXCEPTIONAL),
    (0.90, Tier.VERY_STRONG),
    (0.80, Tier.STRONG),
    (0.65, Tier.MODERATE),
    (0.0, Tier.WEAK),
)


@dataclass(frozen=True)
class Component:
    """One measured axis of readiness, on 0.0–1.0.

    `weight` of 0.0 means this could not be measured. It still appears, and
    `finding` says why — a component that vanishes from the screen when its
    input is missing reads as one that passed.
    """

    name: str
    score: float
    weight: float
    finding: str

    @property
    def measured(self) -> bool:
        return self.weight > 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "score": round(self.score, 3),
            "weight": self.weight,
            "finding": self.finding,
            "measured": self.measured,
        }


@dataclass(frozen=True)
class Blocker:
    """A reason this application cannot be sent, whatever it scored.

    `detail` is shown to the owner verbatim, so it names the specific thing —
    the question that has no answer, the field that is empty — rather than the
    category. "A required question is unanswered" sends the owner looking;
    quoting the question does not.
    """

    code: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class Readiness:
    """The composite, its parts, and what is stopping it."""

    components: list[Component] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)

    @property
    def measured(self) -> list[Component]:
        return [c for c in self.components if c.measured]

    @property
    def score(self) -> float | None:
        """Weighted mean over the measured components, or None.

        None means "not enough was measurable to say", which is a different
        answer from a low score and must not be rendered as one.
        """
        measured = self.measured
        if len(measured) < MIN_COMPONENTS:
            return None
        total = sum(c.weight for c in measured)
        if not total:
            return None
        return round(sum(c.score * c.weight for c in measured) / total, 3)

    @property
    def tier(self) -> Tier | None:
        value = self.score
        if value is None:
            return None
        return next(tier for floor, tier in _BANDS if value >= floor)

    @property
    def is_ready(self) -> bool:
        """§53's gate. False on any blocker, and on an unassessed application."""
        if self.blockers:
            return False
        value = self.score
        return value is not None and value >= READY_FLOOR

    @property
    def weakest(self) -> Component | None:
        """The measured component dragging this down — worth reading first."""
        measured = self.measured
        return min(measured, key=lambda c: c.score) if measured else None

    def summary(self) -> str:
        if self.blockers:
            reasons = "; ".join(b.detail for b in self.blockers)
            return f"not ready — {reasons}"
        value = self.score
        if value is None:
            named = ", ".join(c.name for c in self.components if not c.measured)
            return f"not assessed — too little measured ({named or 'no components'})"
        return f"{self.tier} ({value:.0%})" + ("" if self.is_ready else " — below the floor")

    def as_dict(self) -> dict[str, object]:
        value = self.score
        tier = self.tier
        weakest = self.weakest
        return {
            "score": value,
            "tier": tier.value if tier else None,
            "ready": self.is_ready,
            "components": [c.as_dict() for c in self.components],
            "blockers": [b.as_dict() for b in self.blockers],
            "weakest": weakest.name if weakest else None,
            "summary": self.summary(),
        }


def _match_component(rubric_overall: float | None) -> Component:
    """The match rubric, normalised off its 1–5 scale onto 0–1.

    `(x - 1) / 4` rather than `x / 5`: on the rubric's own scale 1.0 is the
    floor and means excluded, so dividing by 5 would report a hard-filtered
    posting as 20% fit rather than 0%.
    """
    if rubric_overall is None:
        return Component("match", 0.0, 0.0, "no match score for this posting")
    normalised = max(0.0, min(1.0, (rubric_overall - 1.0) / 4.0))
    return Component("match", normalised, 0.30, f"rubric {rubric_overall:.2f}/5")


def _ats_component(parse: float | None, keywords: float | None) -> Component:
    """How a machine reads the document.

    Weighted toward parse for the reason `AtsReport.overall` is: a parser that
    cannot find the Experience section never reaches the keywords inside it.
    With no posting only parse is meaningful, and it is still worth reporting —
    an unparseable résumé is unparseable whatever job it is aimed at.
    """
    if parse is None:
        return Component("ats", 0.0, 0.0, "résumé not scored for machine parsing")
    if keywords is None:
        return Component("ats", parse, 0.20, f"parse {parse:.0%}, no posting to match against")
    return Component(
        "ats", parse * 0.6 + keywords * 0.4, 0.20, f"parse {parse:.0%}, keywords {keywords:.0%}"
    )


def _recruiter_component(overall: float | None, shortlist: str | None) -> Component:
    """Whether a person would shortlist it."""
    if overall is None:
        return Component("recruiter", 0.0, 0.0, "résumé not read as a recruiter would")
    return Component("recruiter", overall, 0.30, f"{shortlist or 'scored'} ({overall:.0%})")


def _form_component(fill_rate: float | None) -> Component:
    """How much of the real form this profile could answer.

    Distinct from the blockers below it: an unanswered *required* question
    blocks outright, while a form three-quarters filled with only optional
    gaps is sendable and merely weaker than one filled completely.
    """
    if fill_rate is None:
        return Component("form", 0.0, 0.0, "form not yet enumerated")
    return Component(
        "form", max(0.0, min(1.0, fill_rate)), 0.20, f"{fill_rate:.0%} of fields filled"
    )


def assess(
    *,
    rubric_overall: float | None = None,
    ats_parse: float | None = None,
    ats_keywords: float | None = None,
    recruiter_overall: float | None = None,
    recruiter_shortlist: str | None = None,
    fill_rate: float | None = None,
    unanswered_required: list[str] | None = None,
    knock_outs: list[str] | None = None,
    missing_profile_fields: list[str] | None = None,
    excluded_by: list[str] | None = None,
    has_resume: bool = True,
) -> Readiness:
    """Compose what has already been computed. This measures nothing itself.

    Every argument is optional and `None` means "not measured", which is why
    the signature is keyword-only: a positional call site that gained an
    argument would silently shift a score into a different axis.

    Nothing here fetches, parses, embeds or asks a model. That is deliberate —
    it makes the composite testable without a database or a browser, and it
    keeps the numbers the property of the modules that own them. A readiness
    score that recomputed the ATS score would be a second opinion drifting
    against `ats.py`, which CLAUDE.md §15 records happening once already with
    two definitions of "remote".
    """
    components = [
        _match_component(rubric_overall),
        _ats_component(ats_parse, ats_keywords),
        _recruiter_component(recruiter_overall, recruiter_shortlist),
        _form_component(fill_rate),
    ]

    blockers: list[Blocker] = []

    # Ordered most-disqualifying first, because the summary line quotes them in
    # order and the owner should read the reason the application cannot be
    # completed before the reason it would land badly.
    for field_name in missing_profile_fields or []:
        blockers.append(Blocker("incomplete_profile", f"{field_name} is not set"))

    if not has_resume:
        blockers.append(Blocker("no_resume", "no résumé is attached to this application"))

    for question in unanswered_required or []:
        blockers.append(Blocker("unanswered_required", f"required question unanswered: {question}"))

    for question in knock_outs or []:
        blockers.append(Blocker("knock_out", f"likely disqualifying: {question}"))

    for reason in excluded_by or []:
        blockers.append(Blocker("hard_filter", f"excluded by a hard filter: {reason}"))

    return Readiness(components=components, blockers=blockers)
