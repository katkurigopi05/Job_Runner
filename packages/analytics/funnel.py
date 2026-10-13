"""Does a higher match score actually produce a reply?

The project has scored every posting it has ever seen and recorded what the
employer did afterwards, and has never once compared the two. That is the
feedback loop `docs/REFERENCE.md` §3.5 named as missing: a scorer nobody
checks is a scorer that can drift for months while every dashboard stays
green.

Two questions, both answerable from rows already in the database.

**Where does the pipeline lose applications?** Counting by stage says whether
the constraint is discovery, the approval queue, or employers not replying —
three problems with nothing in common except that they all look like "not
enough interviews".

**Do high scores do better than low ones?** Applications are bucketed by the
score that got them queued, and each bucket reports its own reply rate. This
deliberately reports *rates and counts side by side* rather than a single
correlation number: on a personal job search the sample is small enough that
one interview in a three-application bucket reads as a 33% success rate, and a
number without its denominator invites exactly that mistake.

Nothing here decides anything. It is a report — §2 has no rule that a score
may act on its own, and this does not add one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.enums import OUTCOME_FOR_CLASSIFICATION, ApplicationStatus, Outcome
from packages.core.models import Application, InboundMessage, Match

#: Score buckets, low to high. Half-open on the right except the last.
#: Chosen to match how the feed is actually read — "below a coin flip",
#: "plausible", "good", "the ones you would apply to by hand".
SCORE_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("0.00–0.50", 0.0, 0.50),
    ("0.50–0.65", 0.50, 0.65),
    ("0.65–0.80", 0.65, 0.80),
    ("0.80–1.00", 0.80, 1.01),
)

#: Outcomes that mean a human at the employer engaged. `acknowledged` is
#: excluded on purpose: an automated "we received your application" is not a
#: reply, and counting it would make every bucket look successful.
#:
#: An assessment is in it. `Outcome.ASSESSMENT` was added to the enum and to
#: none of the sets here or in `cadence.py`, so an employer who sent a timed
#: exercise had not answered, had not engaged, and was listed as silent and
#: owed a follow-up. `tests/test_analytics.py` now fails on an outcome that is
#: in none of the three groups, or in two.
ENGAGED: frozenset[str] = frozenset(
    {
        Outcome.INTERVIEW.value,
        Outcome.OFFER.value,
        Outcome.INFO_REQUESTED.value,
        Outcome.ASSESSMENT.value,
    }
)

#: Nothing from a person yet: no reply, or the automated receipt.
NOT_AN_ANSWER: frozenset[str] = frozenset({Outcome.AWAITING.value, Outcome.ACKNOWLEDGED.value})

#: A rejection is a real answer — it means a person read it. Kept apart from
#: ENGAGED because the two say opposite things about whether to apply again.
ANSWERED: frozenset[str] = ENGAGED | {Outcome.REJECTED.value}

#: What a message is classified as when it means one of ENGAGED.
_ENGAGING: frozenset[str] = frozenset(
    classification.value
    for classification, outcome in OUTCOME_FOR_CLASSIFICATION.items()
    if outcome.value in ENGAGED
)


@dataclass(frozen=True)
class StageCounts:
    """How many applications sit at each stage of the funnel."""

    total: int = 0
    submitted: int = 0
    needs_review: int = 0
    failed: int = 0
    #: Submitted and the employer has said something, anything.
    answered: int = 0
    #: Submitted and a person engaged — interview, offer, or a request.
    engaged: int = 0

    @property
    def answer_rate(self) -> float | None:
        """Share of submitted applications that got any reply.

        None rather than 0.0 when nothing has been submitted. A rate computed
        over an empty denominator is not zero, it is unknown, and showing 0%
        would read as "everyone ignores you" on a fresh install.
        """
        return self.answered / self.submitted if self.submitted else None

    @property
    def engagement_rate(self) -> float | None:
        return self.engaged / self.submitted if self.submitted else None


@dataclass(frozen=True)
class ScoreBucket:
    """One score band, and how the applications in it fared."""

    label: str
    low: float
    high: float
    applications: int = 0
    submitted: int = 0
    answered: int = 0
    engaged: int = 0

    @property
    def engagement_rate(self) -> float | None:
        return self.engaged / self.submitted if self.submitted else None

    @property
    def is_meaningful(self) -> bool:
        """Whether this bucket holds enough applications to read a rate from.

        Five is not a statistical threshold and does not pretend to be. It is
        the point below which a single reply swings the rate by twenty points
        or more, which is the failure this flag exists to mark.
        """
        return self.submitted >= 5


@dataclass
class FunnelReport:
    stages: StageCounts = field(default_factory=StageCounts)
    buckets: list[ScoreBucket] = field(default_factory=list)
    #: Applications with no Match row — usually applied to by hand or by URL.
    unscored: int = 0
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def score_tracks_outcome(self) -> bool | None:
        """Whether higher-scoring applications engaged more often.

        Compares the top bucket that holds enough applications against the
        bottom one. None when fewer than two buckets qualify — which is the
        honest answer for most of this tool's life, and much better than a
        confident verdict drawn from four applications.
        """
        usable = [b for b in self.buckets if b.is_meaningful and b.engagement_rate is not None]
        if len(usable) < 2:
            return None
        lowest, highest = usable[0], usable[-1]
        assert lowest.engagement_rate is not None and highest.engagement_rate is not None
        return highest.engagement_rate > lowest.engagement_rate


def _bucket_for(score: float) -> str | None:
    for label, low, high in SCORE_BUCKETS:
        if low <= score < high:
            return label
    return None


async def build(session: AsyncSession, *, profile_id: str | None = None) -> FunnelReport:
    """Count the funnel and the score buckets. Reads only; commits nothing."""
    query = select(Application)
    if profile_id is not None:
        query = query.where(Application.profile_id == profile_id)
    applications = list((await session.scalars(query)).all())

    ever_engaged = await _ever_engaged(session, applications)
    stages = _count_stages(applications, ever_engaged)

    # One query for every relevant score rather than one per application.
    scores = await _scores_for(session, applications)

    tallies: dict[str, dict[str, int]] = {
        label: {"applications": 0, "submitted": 0, "answered": 0, "engaged": 0}
        for label, _, _ in SCORE_BUCKETS
    }
    unscored = 0

    for application in applications:
        score = scores.get((application.profile_id, application.posting_id))
        if score is None:
            unscored += 1
            continue
        label = _bucket_for(score)
        if label is None:
            continue
        tally = tallies[label]
        tally["applications"] += 1
        if application.status == ApplicationStatus.SUBMITTED.value:
            tally["submitted"] += 1
            if application.outcome in ANSWERED:
                tally["answered"] += 1
            if _engaged(application, ever_engaged):
                tally["engaged"] += 1

    buckets = [
        ScoreBucket(label=label, low=low, high=high, **tallies[label])
        for label, low, high in SCORE_BUCKETS
    ]

    return FunnelReport(stages=stages, buckets=buckets, unscored=unscored)


async def _ever_engaged(session: AsyncSession, applications: list[Application]) -> set[object]:
    """The applications an employer engaged with at any point, whatever came after.

    `Application.outcome` is the latest and most final thing an employer said:
    `inbox/route.py` only ever moves it to something at least as definitive,
    and a rejection outranks an interview. Most interviews end in one. Counted
    from `outcome` alone, the applications that got furthest were the ones the
    engagement rate lost, in the report that asks whether a high score earns a
    reply.

    The messages are the history. Only one tied to its application by the
    alias counts, for the reason the router gives: an inferred link is a guess
    about which application a message belongs to, and may conclude nothing.
    """
    ids = [application.id for application in applications]
    if not ids:
        return set()
    rows = await session.scalars(
        select(InboundMessage.application_id)
        .where(
            InboundMessage.application_id.in_(ids),
            InboundMessage.link_method == "alias",
            InboundMessage.classification.in_(_ENGAGING),
        )
        .distinct()
    )
    return set(rows.all())


def _engaged(application: Application, ever_engaged: set[object]) -> bool:
    return application.outcome in ENGAGED or application.id in ever_engaged


def _count_stages(applications: list[Application], ever_engaged: set[object]) -> StageCounts:
    submitted = [a for a in applications if a.status == ApplicationStatus.SUBMITTED.value]
    return StageCounts(
        total=len(applications),
        submitted=len(submitted),
        needs_review=sum(
            1 for a in applications if a.status == ApplicationStatus.NEEDS_REVIEW.value
        ),
        failed=sum(1 for a in applications if a.status == ApplicationStatus.FAILED.value),
        answered=sum(1 for a in submitted if a.outcome in ANSWERED),
        engaged=sum(1 for a in submitted if _engaged(a, ever_engaged)),
    )


async def _scores_for(
    session: AsyncSession, applications: list[Application]
) -> dict[tuple[object, object], float]:
    """Match scores keyed by (profile, posting), for the applications given."""
    posting_ids = {a.posting_id for a in applications if a.posting_id is not None}
    if not posting_ids:
        return {}

    rows = await session.execute(
        select(Match.profile_id, Match.posting_id, Match.score).where(
            Match.posting_id.in_(posting_ids)
        )
    )
    # A posting can be scored against several profiles; keying on both keeps
    # one profile's score off another profile's application.
    return {(profile_id, posting_id): score for profile_id, posting_id, score in rows}
