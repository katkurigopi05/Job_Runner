"""Noting a company a worker could not read, and closing the note when it can.

The worker does not stop on a company it cannot read, and never did: each
company is its own task. What it did not do was keep a list. A failed
discovery left its reason on the company row, and a board that stopped
answering only backed itself off, retried every few hours for ever with no
one told (seven boards returning HTTP 404 on 2026-10-19). Here both are noted
in `company_issues`, one open row per company and stage, for a deep dive to
take once nothing else is waiting (`deep_dive.py`, `DEEP_DIVE_TASK_KIND`).

An ordinary run that succeeds later closes the note: a site that publishes a
board next month needs no one to tidy up after it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import cast, func, select, text, update
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import QueueTask
from packages.core.models_issues import CompanyIssue
from packages.core.queue import enqueue
from packages.crawler.crawl import CompanyResult

#: One company's deep dive, as a queue task. Claimed after every other kind,
#: discovery included (`apps/worker/run.py`, `YIELDING_KINDS`).
DEEP_DIVE_TASK_KIND = "deep_dive_company"

#: Crawls of a known board that must fail in a row before it is noted. One is
#: a bad minute on someone else's server; three, about a day apart once
#: backed off, is a board that has moved or gone.
CRAWL_FAILURES_BEFORE_NOTED = 3

#: Discovery outcomes that are not a problem with the company. A board that
#: lists nothing is a company not hiring today, and is retried on schedule.
NOT_AN_ISSUE = frozenset({"board_empty"})


def crawl_category(result: CompanyResult) -> str | None:
    """One word for why a crawl of a known board failed, or None if it did not."""
    if result.blocked:
        return "board_blocked"
    if result.error:
        return "board_gone" if result.error in ("HTTP 404", "HTTP 410") else "board_error"
    if result.suspect_parse:
        return "board_reads_empty"
    return None


async def note(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    stage: str,
    category: str,
    reason: str,
    now: datetime | None = None,
) -> None:
    """Put a company on the noted list, or bump its open note. Does not commit.

    One statement, so two workers noting the same company cannot open two
    rows (`uq_company_issues_open`). A note the deep dive already gave up on
    (`needs_owner`) is offered to it again only when the failure changed kind:
    the same failure next week would get the same answer.
    """
    current = now or datetime.now(UTC)
    statement = insert(CompanyIssue).values(
        company_id=company_id,
        stage=stage,
        category=category,
        reason=reason,
        status="noted",
        noted_at=current,
        last_noted_at=current,
    )
    table = CompanyIssue.__table__
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["company_id", "stage"],
            index_where=text("status <> 'fixed'"),
            set_={
                "times_noted": table.c.times_noted + 1,
                "last_noted_at": current,
                "reason": statement.excluded.reason,
                "category": statement.excluded.category,
                "status": text(
                    "CASE WHEN company_issues.status = 'needs_owner' "
                    "AND company_issues.category <> excluded.category "
                    "THEN 'noted' ELSE company_issues.status END"
                ),
            },
        )
    )


async def resolve(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    stage: str,
    by: str,
    now: datetime | None = None,
) -> bool:
    """Close a company's open note at this stage. True if there was one."""
    result = await session.execute(
        update(CompanyIssue)
        .where(
            CompanyIssue.company_id == company_id,
            CompanyIssue.stage == stage,
            CompanyIssue.status != "fixed",
        )
        .values(status="fixed", resolved_at=now or datetime.now(UTC), resolved_by=by)
        .returning(CompanyIssue.id)
    )
    return result.first() is not None


def _owed_a_deep_dive() -> Any:
    """A note with no deep dive waiting or running for it, and none done.

    `noted`, or `queued` with its task gone: cancelled, failed for good, or
    lost with its worker. The live ids are compared with NOT IN over a
    subquery that never yields NULL, which Postgres hashes once.
    """
    live = select(cast(QueueTask.payload_json["issue_id"].astext, PgUUID)).where(
        QueueTask.kind == DEEP_DIVE_TASK_KIND,
        QueueTask.status.in_(("pending", "running")),
        QueueTask.payload_json["issue_id"].astext.is_not(None),
    )
    return (CompanyIssue.status == "noted") | (
        (CompanyIssue.status == "queued") & CompanyIssue.id.not_in(live)
    )


async def start_deep_dives(
    session: AsyncSession, *, limit: int | None = None, now: datetime | None = None
) -> list[uuid.UUID]:
    """Enqueue a deep dive for each note owed one, oldest first. Does not commit.

    **Only with the owner's permission** (2026-10-19): the noted companies are
    looked into at the end, when they say so. This is what `make deep-dive`
    and `POST /company-issues/deep-dive` call, and the tick calls it only
    while `DEEP_DIVE_AUTO` is set. The first build dispatched on every tick,
    and 2,343 deep dives were queued to start overnight without asking.

    Locked and skipped, so two callers at once never start one note twice.
    """
    if limit is not None and limit <= 0:
        return []
    current = now or datetime.now(UTC)
    query = (
        select(CompanyIssue.id)
        .where(_owed_a_deep_dive())
        .order_by(CompanyIssue.noted_at)
        .with_for_update(skip_locked=True)
    )
    if limit is not None:
        query = query.limit(limit)
    ids = list((await session.scalars(query)).all())
    if ids:
        await session.execute(
            update(CompanyIssue)
            .where(CompanyIssue.id.in_(ids))
            .values(status="queued", queued_at=current)
        )
        for issue_id in ids:
            await enqueue(session, DEEP_DIVE_TASK_KIND, {"issue_id": str(issue_id)})
    return ids


@dataclass(frozen=True)
class IssueSummary:
    """The noted list at a glance."""

    noted: int
    queued: int
    needs_owner: int
    fixed: int
    #: Notes a deep dive would be started for now: the ones awaiting permission.
    waiting_for_permission: int


async def summary(session: AsyncSession) -> IssueSummary:
    counts = dict(
        (
            await session.execute(
                select(CompanyIssue.status, func.count()).group_by(CompanyIssue.status)
            )
        )
        .tuples()
        .all()
    )
    waiting = await session.scalar(
        select(func.count()).select_from(CompanyIssue).where(_owed_a_deep_dive())
    )
    return IssueSummary(
        noted=counts.get("noted", 0),
        queued=counts.get("queued", 0),
        needs_owner=counts.get("needs_owner", 0),
        fixed=counts.get("fixed", 0),
        waiting_for_permission=int(waiting or 0),
    )


async def deep_dive_backlog(session: AsyncSession) -> int:
    """Deep dives enqueued and not yet finished."""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(QueueTask)
            .where(
                QueueTask.kind == DEEP_DIVE_TASK_KIND,
                QueueTask.status.in_(("pending", "running")),
            )
        )
        or 0
    )


__all__ = [
    "CRAWL_FAILURES_BEFORE_NOTED",
    "DEEP_DIVE_TASK_KIND",
    "NOT_AN_ISSUE",
    "IssueSummary",
    "crawl_category",
    "deep_dive_backlog",
    "note",
    "resolve",
    "start_deep_dives",
    "summary",
]
