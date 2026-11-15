"""Manual rescoring uses the durable queue, without making an HTTP request wait."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.enums import QueueTaskStatus
from packages.core.models import QueueTask
from packages.core.queue import ClaimedTask, enqueue
from packages.matching.rescore import rescore

RESCORE_TASK_KIND = "rescore"
_QUEUE_GUARD_LOCK_KEY = 4_917_207


async def request_rescore(session: AsyncSession) -> bool:
    """Queue at most one unfinished all-profile rescore; caller commits.

    A transaction lock protects concurrent HTTP requests, including requests
    served by different API processes. A replay recomputes the same Match rows.
    """
    await session.execute(select(func.pg_advisory_xact_lock(_QUEUE_GUARD_LOCK_KEY)))
    waiting = await session.scalar(
        select(QueueTask.id)
        .where(
            QueueTask.kind == RESCORE_TASK_KIND,
            QueueTask.status.in_((QueueTaskStatus.PENDING.value, QueueTaskStatus.RUNNING.value)),
        )
        .limit(1)
    )
    if waiting is not None:
        return False
    await enqueue(session, RESCORE_TASK_KIND, {})
    return True


async def handle_rescore(session: AsyncSession, claimed: ClaimedTask) -> None:
    """Reuse CLI scoring, with transaction/timeout/retry owned by the worker."""
    await rescore(session)
