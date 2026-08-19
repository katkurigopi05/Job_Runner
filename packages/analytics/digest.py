"""A week in one object.

`weekly-digest` is the one thing in career-ops that composes rather than
computes: it asks the other reports for their numbers and arranges them. This
does the same, and exists for the same reason — the owner of a job-search tool
does not want to read four dashboards on a Monday.

Everything here is derived. If a number looks wrong, it is wrong in `funnel`,
`cadence`, or the data, and this module is not where to fix it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.analytics import cadence, funnel
from packages.core.enums import ApplicationStatus
from packages.core.models import Application, Posting

DEFAULT_WINDOW_DAYS = 7


@dataclass
class Digest:
    """What happened in the window, and what needs attention because of it."""

    window_days: int = DEFAULT_WINDOW_DAYS
    postings_seen: int = 0
    applications_created: int = 0
    applications_submitted: int = 0
    replies_received: int = 0
    #: Waiting on the owner right now — the only actionable count here.
    awaiting_review: int = 0
    follow_ups_due: int = 0
    funnel: funnel.FunnelReport = field(default_factory=funnel.FunnelReport)
    latency: cadence.LatencyReport = field(default_factory=cadence.LatencyReport)
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def quiet_week(self) -> bool:
        """Nothing came in and nothing went out.

        Worth naming rather than leaving the reader to infer it from six
        zeroes: a quiet week usually means the crawler stopped, not that the
        market did.
        """
        return self.postings_seen == 0 and self.applications_created == 0


async def build(
    session: AsyncSession,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
    profile_id: str | None = None,
) -> Digest:
    """Assemble the digest. Reads only."""
    current = now or datetime.now(UTC)
    since = current - timedelta(days=window_days)

    postings_count = (
        await session.scalar(
            select(func.count()).select_from(Posting).where(Posting.first_seen_at >= since)
        )
    ) or 0

    app_query = select(Application).where(Application.created_at >= since)
    if profile_id is not None:
        app_query = app_query.where(Application.profile_id == profile_id)
    recent = list((await session.scalars(app_query)).all())

    review_query = select(Application).where(
        Application.status == ApplicationStatus.NEEDS_REVIEW.value
    )
    if profile_id is not None:
        review_query = review_query.where(Application.profile_id == profile_id)
    awaiting = len((await session.scalars(review_query)).all())

    replies = len(
        [
            application
            for application in recent
            if application.outcome_at is not None and application.outcome_at >= since
        ]
    )

    silence = await cadence.silence(session, now=current, profile_id=profile_id)

    return Digest(
        window_days=window_days,
        postings_seen=postings_count,
        applications_created=len(recent),
        applications_submitted=sum(
            1 for a in recent if a.status == ApplicationStatus.SUBMITTED.value
        ),
        replies_received=replies,
        awaiting_review=awaiting,
        follow_ups_due=len(silence.due),
        funnel=await funnel.build(session, profile_id=profile_id),
        latency=await cadence.latency(session, profile_id=profile_id),
    )
