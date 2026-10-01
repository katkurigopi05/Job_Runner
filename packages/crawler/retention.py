"""Delete the postings the owner no longer searches.

The owner searches jobs posted in the last month. On 2026-09-30 the postings
table held 25,738 rows, 19,258 of them older than that or closed, and at 226
MB of a 275 MB database it is what every search scans: the chat retriever's
keyword pass reads every open posting, and the feed reads them all again.

`Settings.posting_max_age_days` stops the crawler storing new old postings;
this removes the ones already held. Without the first, the second is undone
by the next crawl, since an old posting that is still listed comes straight
back as new.

## What is never deleted

A posting the owner did something with: applied to, swiped on, tailored a
résumé for, or graded. Each of those rows points at its posting, and deleting
the posting would not just lose context:

- `applications.posting_id` and `resumes.tailored_for_posting_id` are
  `ON DELETE SET NULL`. A tailored résumé whose posting is gone reads exactly
  like a base résumé, the one thing `pick_resume.py` exists to tell apart, so
  it could be chosen as the starting point for an unrelated application.
- `matches` and `posting_labels` are `ON DELETE CASCADE`. A swipe or a grade
  would vanish with the posting, and labels are what every ranking claim in
  this repo is waiting on.

`rescore` already draws the same line for stale match rows: withdrawn only
when the row holds nothing of the owner's.

A posting with no date is never deleted for its age: an unknown age is not
evidence of an old one. If it is closed, it goes like any closed posting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, and_, delete, exists, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Application, Match, Posting, PostingLabel, Resume


@dataclass(frozen=True)
class PruneReport:
    #: Postings that are closed, or older than the window.
    stale: int
    #: Stale postings kept because the owner did something with them.
    kept_for_owner: int
    #: Postings deleted. Zero on a dry run.
    deleted: int
    #: Postings left afterwards (or that would be, on a dry run).
    remaining: int


def _stale(cutoff: datetime) -> ColumnElement[bool]:
    return or_(Posting.closed_at.is_not(None), Posting.published_at < cutoff)


def _owned() -> ColumnElement[bool]:
    """The owner did something with this posting. See the module docstring."""
    return or_(
        exists().where(Application.posting_id == Posting.id),
        exists().where(Resume.tailored_for_posting_id == Posting.id),
        exists().where(PostingLabel.posting_id == Posting.id),
        exists().where(
            Match.posting_id == Posting.id,
            or_(Match.decision.is_not(None), Match.tailored_resume_id.is_not(None)),
        ),
    )


async def prune(
    session: AsyncSession, *, max_age_days: int, now: datetime, apply: bool
) -> PruneReport:
    """Delete closed and old postings the owner never touched. Does not commit.

    `max_age_days` of 0 keeps every age, so only closed postings go.
    """
    cutoff = (
        now - timedelta(days=max_age_days)
        if max_age_days > 0
        else datetime.min.replace(tzinfo=now.tzinfo)
    )
    stale = _stale(cutoff)

    async def count(*where: ColumnElement[bool]) -> int:
        return int(
            await session.scalar(select(func.count()).select_from(Posting).where(*where)) or 0
        )

    total = await count()
    stale_count = await count(stale)
    kept = await count(stale, _owned())
    deleted = 0
    if apply:
        result = await session.execute(delete(Posting).where(and_(stale, not_(_owned()))))
        deleted = int(getattr(result, "rowcount", 0) or 0)
    return PruneReport(
        stale=stale_count,
        kept_for_owner=kept,
        deleted=deleted,
        remaining=total - (deleted if apply else stale_count - kept),
    )


__all__ = ["PruneReport", "prune"]
