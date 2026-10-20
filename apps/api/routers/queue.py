"""The task queue: what it is doing, and withdrawing a task before it runs.

Added on 2026-10-19 with the queue's retry backoff, time limits and pruning
(`packages/core/queue.py`). Read-only apart from cancelling, which only ever
touches a task that has not started.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, Query
from sqlalchemy import select

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from packages.core.enums import ErrorCode, QueueTaskStatus
from packages.core.models import QueueTask
from packages.core.queue import cancel_pending, queue_stats
from packages.core.schemas_queue import (
    CancelIn,
    CancelKindIn,
    CancelKindOut,
    KindStatsOut,
    QueueTaskOut,
)

router = APIRouter(prefix="/queue", tags=["queue"])

#: Kinds whose loss breaks something else, and what to do instead.
NOT_CANCELLABLE: dict[str, str] = {
    "apply": "an apply task is withdrawn by rejecting the application",
    "crawl": "the crawl tick schedules the next one itself; cancelling it stops crawling",
    "inbox": "the inbox poll schedules the next one itself; cancelling it stops mail",
}


@router.get("/stats", response_model=list[KindStatsOut])
async def stats(
    session: SessionDep, hours: float = Query(default=24, gt=0, le=24 * 30)
) -> list[KindStatsOut]:
    """Each kind of task: how many wait and run now, and what finished lately."""
    rows = await queue_stats(session, since=timedelta(hours=hours))
    return [KindStatsOut.model_validate(row) for row in rows]


@router.get("/tasks", response_model=list[QueueTaskOut])
async def tasks(
    session: SessionDep,
    status: str | None = None,
    kind: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[QueueTaskOut]:
    """Tasks, newest first, by status and kind."""
    query = select(QueueTask).order_by(QueueTask.created_at.desc()).limit(limit)
    if status:
        query = query.where(QueueTask.status == status)
    if kind:
        query = query.where(QueueTask.kind == kind)
    return [QueueTaskOut.model_validate(task) for task in (await session.scalars(query)).all()]


@router.post("/tasks/{task_id}/cancel", response_model=QueueTaskOut)
async def cancel_task(
    session: SessionDep, task_id: uuid.UUID, body: CancelIn | None = None
) -> QueueTaskOut:
    """Cancel one task that has not started. The body, a reason, is optional."""
    reason = (body or CancelIn()).reason
    task = await session.get(QueueTask, task_id)
    if task is None:
        raise ApiError(ErrorCode.NOT_FOUND, "task not found")
    if task.kind in NOT_CANCELLABLE:
        raise ApiError(ErrorCode.INVALID_STATE, NOT_CANCELLABLE[task.kind])
    if task.status != QueueTaskStatus.PENDING.value:
        raise ApiError(
            ErrorCode.INVALID_STATE,
            f"task is {task.status}; only a task that has not started can be cancelled",
        )
    cancelled = await cancel_pending(
        session, task_id=task_id, reason=reason, refuse_kinds=NOT_CANCELLABLE
    )
    if not cancelled:
        # Claimed between the read and the update.
        raise ApiError(ErrorCode.INVALID_STATE, "task started before it could be cancelled")
    await session.commit()
    await session.refresh(task)
    return QueueTaskOut.model_validate(task)


@router.post("/cancel", response_model=CancelKindOut)
async def cancel_kind(session: SessionDep, body: CancelKindIn) -> CancelKindOut:
    """Cancel every task of one kind that has not started."""
    if body.kind in NOT_CANCELLABLE:
        raise ApiError(ErrorCode.INVALID_STATE, NOT_CANCELLABLE[body.kind])
    cancelled = await cancel_pending(
        session, kind=body.kind, reason=body.reason, refuse_kinds=NOT_CANCELLABLE
    )
    await session.commit()
    return CancelKindOut(kind=body.kind, cancelled=len(cancelled))
