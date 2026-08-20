"""Which applications have gone quiet, and how long employers take to answer.

Two reports that need no new data at all — `created_at`, `outcome`, and
`outcome_at` have been on every application row since Phase 0.

**Silence.** An application submitted three weeks ago with no reply is the
single most common state in a job search and the easiest to lose track of,
because nothing ever happens to it. Nothing in this project surfaced it: the
pipeline board shows `submitted` and stops, which is indistinguishable from
`submitted and forgotten`.

**Rejection latency.** How long employers actually take, measured from this
owner's own applications. Its real use is calibrating the silence threshold:
if the median answer arrives in 9 days, chasing at 7 is early, and if it
arrives in 30, a 14-day follow-up is noise. A default that ignores the data
sitting in the table is a guess wearing a number.

Deliberately not here: sending anything. This reports who has gone quiet. The
owner writes the email — §2 gives no rule permitting this project to
correspond with an employer on its own, and a follow-up sent by a machine is
exactly the kind of thing that should stay the owner's decision.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.enums import ApplicationStatus, Outcome
from packages.core.models import Application

#: When an application with no reply becomes worth chasing. Two weeks is the
#: common convention, and `latency()` is how you find out whether it is right
#: for the boards this owner actually applies to.
DEFAULT_SILENT_AFTER_DAYS = 14

#: Past this, a follow-up is unlikely to land and the row is better read as
#: closed. Nothing is written — the application keeps its real outcome,
#: because "we never heard back" is not the same as "we were rejected" and
#: recording it as a rejection would corrupt the funnel's own numbers.
DEFAULT_STALE_AFTER_DAYS = 45

#: Outcomes that mean the employer has answered, so silence has ended.
_ANSWERED: frozenset[str] = frozenset(
    {
        Outcome.INTERVIEW.value,
        Outcome.OFFER.value,
        Outcome.REJECTED.value,
        Outcome.INFO_REQUESTED.value,
        Outcome.ACKNOWLEDGED.value,
    }
)


@dataclass(frozen=True)
class SilentApplication:
    """A submitted application the employer has not answered."""

    application_id: str
    url: str
    days_since: int
    #: True once a follow-up is unlikely to be worth sending.
    stale: bool

    @property
    def due(self) -> bool:
        return not self.stale


@dataclass
class CadenceReport:
    silent: list[SilentApplication] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def due(self) -> list[SilentApplication]:
        """Worth following up now, oldest first."""
        return [item for item in self.silent if item.due]

    @property
    def stale(self) -> list[SilentApplication]:
        return [item for item in self.silent if item.stale]


@dataclass(frozen=True)
class LatencyReport:
    """How long employers took to answer, from this owner's own history."""

    samples: int = 0
    median_days: float | None = None
    fastest_days: int | None = None
    slowest_days: int | None = None
    #: Split out because a rejection and an interview invitation travel at
    #: very different speeds, and averaging them describes neither.
    median_rejection_days: float | None = None
    median_engagement_days: float | None = None

    @property
    def suggested_silent_after_days(self) -> int | None:
        """A follow-up threshold derived from what actually happened.

        The median plus a week: past the point most employers have answered,
        with enough margin that a normally slow one is not chased. None when
        there is too little history to say anything, which is the honest
        answer until a dozen applications have been answered.
        """
        if self.samples < 5 or self.median_days is None:
            return None
        return int(round(self.median_days)) + 7


def _days_between(start: datetime, end: datetime) -> int:
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return max((end - start).days, 0)


async def silence(
    session: AsyncSession,
    *,
    silent_after_days: int = DEFAULT_SILENT_AFTER_DAYS,
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
    now: datetime | None = None,
    profile_id: str | None = None,
) -> CadenceReport:
    """Submitted applications with no reply, oldest first."""
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(days=silent_after_days)

    query = select(Application).where(
        Application.status == ApplicationStatus.SUBMITTED.value,
        Application.created_at <= cutoff,
    )
    if profile_id is not None:
        query = query.where(Application.profile_id == profile_id)

    silent: list[SilentApplication] = []
    for application in (await session.scalars(query)).all():
        # `awaiting` and NULL both mean nothing has come back. Anything in
        # _ANSWERED means the employer spoke, so this is not silence.
        if application.outcome in _ANSWERED:
            continue
        days = _days_between(application.created_at, current)
        silent.append(
            SilentApplication(
                application_id=str(application.id),
                url=application.url,
                days_since=days,
                stale=days >= stale_after_days,
            )
        )

    silent.sort(key=lambda item: item.days_since, reverse=True)
    return CadenceReport(silent=silent)


async def latency(session: AsyncSession, *, profile_id: str | None = None) -> LatencyReport:
    """How long answers took, measured from this owner's own applications."""
    query = select(Application).where(Application.outcome_at.is_not(None))
    if profile_id is not None:
        query = query.where(Application.profile_id == profile_id)

    all_days: list[int] = []
    rejection_days: list[int] = []
    engagement_days: list[int] = []

    for application in (await session.scalars(query)).all():
        if application.outcome_at is None or application.outcome is None:
            continue
        days = _days_between(application.created_at, application.outcome_at)
        all_days.append(days)
        if application.outcome == Outcome.REJECTED.value:
            rejection_days.append(days)
        elif application.outcome in {Outcome.INTERVIEW.value, Outcome.OFFER.value}:
            engagement_days.append(days)

    if not all_days:
        return LatencyReport()

    return LatencyReport(
        samples=len(all_days),
        median_days=statistics.median(all_days),
        fastest_days=min(all_days),
        slowest_days=max(all_days),
        median_rejection_days=statistics.median(rejection_days) if rejection_days else None,
        median_engagement_days=statistics.median(engagement_days) if engagement_days else None,
    )
