"""Crawler status — is it working right now, and when did it last finish.

The queue was invisible from the dashboard. `make crawl` enqueues, `make worker`
drains, and nothing on any screen said whether either was happening — so a stale
match feed looked identical to a fresh one with nothing new, and telling them
apart meant opening a terminal and querying Postgres.

That is the blindness `/health` had before it learned to fail: the answer
existed and nothing surfaced it.

Read-only on purpose. This said "starting a crawl stays a deliberate act at a
terminal, because it makes real outbound requests to employers' sites", and
the owner reversed the terminal half on 2026-10-04: typing "run crawler" or
"find new jobs" to the assistant now starts one (`chat.asks_to_crawl`). The
reason survives intact. It is still a deliberate act — a typed command, matched
in code, never inferred from a question — still reachable only from this
machine, and it still goes through `crawl_job.request_crawl`, so robots.txt,
the per-host floors and the one-crawl-at-a-time guard are unchanged. This
router stays read-only.
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import desc, func, select

from apps.api.deps import SessionDep
from apps.worker.crawl_job import CRAWL_TASK_KIND
from packages.core.enums import QueueTaskStatus
from packages.core.models import Posting, QueueTask
from packages.core.schemas import CrawlStatusOut

router = APIRouter(prefix="/crawl", tags=["crawl"])


@router.get("/status", response_model=CrawlStatusOut)
async def crawl_status(session: SessionDep) -> CrawlStatusOut:
    """Whether a crawl is running, waiting, or stuck waiting for a worker."""
    running = await session.scalar(
        select(func.count())
        .select_from(QueueTask)
        .where(
            QueueTask.kind == CRAWL_TASK_KIND,
            QueueTask.status == QueueTaskStatus.RUNNING.value,
        )
    )
    pending = await session.scalar(
        select(func.count())
        .select_from(QueueTask)
        .where(
            QueueTask.kind == CRAWL_TASK_KIND,
            QueueTask.status == QueueTaskStatus.PENDING.value,
        )
    )

    last = (
        await session.scalars(
            select(QueueTask)
            .where(
                QueueTask.kind == CRAWL_TASK_KIND,
                QueueTask.status.in_((QueueTaskStatus.DONE.value, QueueTaskStatus.FAILED.value)),
            )
            # `locked_at` is when the worker claimed it, which is the closest
            # thing the row has to a finish time — there is no completed_at.
            .order_by(desc(QueueTask.locked_at))
            .limit(1)
        )
    ).first()

    return CrawlStatusOut(
        running=bool(running),
        pending=int(pending or 0),
        # Work waiting with nobody holding it. Distinct from running, because
        # the fix differs: this one means `make worker` is not up.
        stalled=bool(pending) and not bool(running),
        last_finished_at=last.locked_at if last else None,
        last_status=last.status if last else None,
        # The number that actually answers "are my postings current". A crawl
        # that ran and found nothing leaves this unchanged, which is the truth.
        newest_posting_at=await session.scalar(select(func.max(Posting.first_seen_at))),
    )
