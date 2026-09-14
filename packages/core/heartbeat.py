"""Worker liveness: the writer the worker calls, and the reading the dashboard shows.

A heartbeat that can fail the worker is worse than none, so every write is
wrapped and logged by class name. A heartbeat that writes on every loop turn
is a query a second per idle worker, so writes are throttled to one per
`HEARTBEAT_INTERVAL_S` unless something changed that the owner would want to
see at once — a task starting, a clean stop.
"""

from __future__ import annotations

import os
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.models_ops import WorkerHeartbeat

log = structlog.get_logger(__name__)

#: How often an idle worker writes. Well inside `STALE_AFTER_S`.
HEARTBEAT_INTERVAL_S = 15.0

#: A worker silent this long is reported stale. Six missed beats: generous
#: enough that one slow query does not flap the dashboard, short enough that
#: a dead worker is visible within two minutes.
STALE_AFTER_S = 90.0


class WorkerState(StrEnum):
    ALIVE = "alive"
    #: Silent past the threshold without having said goodbye — crashed, killed,
    #: or the machine slept.
    STALE = "stale"
    STOPPED = "stopped"


@dataclass(frozen=True)
class WorkerView:
    worker_id: str
    hostname: str
    state: WorkerState
    last_seen_at: datetime
    seconds_since_seen: float
    current_task_kind: str | None
    tasks_completed: int
    last_error_kind: str | None


async def record(
    session: AsyncSession,
    *,
    worker_id: str,
    started_at: datetime,
    task_kind: str | None = None,
    task_id: uuid.UUID | None = None,
    completed: bool = False,
    error_kind: str | None = None,
    stopped: bool = False,
    now: datetime | None = None,
) -> None:
    """Upsert one worker's beat. Commits nothing; the caller owns the session."""
    current = now or datetime.now(UTC)
    values = {
        "worker_id": worker_id,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "started_at": started_at,
        "last_seen_at": current,
        "current_task_kind": task_kind,
        "current_task_id": task_id,
        "tasks_completed": 1 if completed else 0,
        "last_error_kind": error_kind,
        "stopped_at": current if stopped else None,
    }
    statement = pg_insert(WorkerHeartbeat).values(**values)
    excluded = statement.excluded
    statement = statement.on_conflict_do_update(
        index_elements=[WorkerHeartbeat.worker_id],
        set_={
            "hostname": excluded.hostname,
            "pid": excluded.pid,
            "started_at": excluded.started_at,
            "last_seen_at": excluded.last_seen_at,
            "current_task_kind": excluded.current_task_kind,
            "current_task_id": excluded.current_task_id,
            "tasks_completed": WorkerHeartbeat.tasks_completed + (1 if completed else 0),
            # An error is kept until a later beat reports another, so one clean
            # idle loop does not erase the evidence of the failure before it.
            "last_error_kind": (
                excluded.last_error_kind
                if error_kind is not None
                else WorkerHeartbeat.last_error_kind
            ),
            "stopped_at": excluded.stopped_at,
        },
    )
    await session.execute(statement)


class Heartbeat:
    """Throttled beats for one worker, on their own short sessions."""

    def __init__(
        self,
        worker_id: str,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        interval_s: float = HEARTBEAT_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.worker_id = worker_id
        self.started_at = datetime.now(UTC)
        self._sessionmaker = sessionmaker
        self._interval_s = interval_s
        self._clock = clock
        self._last_write: float | None = None

    async def pulse(
        self,
        *,
        force: bool = False,
        task_kind: str | None = None,
        task_id: uuid.UUID | None = None,
        completed: bool = False,
        error_kind: str | None = None,
        stopped: bool = False,
    ) -> bool:
        """Write a beat if one is due or `force`d. Returns whether it wrote. Never raises."""
        now = self._clock()
        due = self._last_write is None or now - self._last_write >= self._interval_s
        if not (force or due or completed or error_kind or stopped):
            return False
        try:
            async with self._sessionmaker() as session:
                await record(
                    session,
                    worker_id=self.worker_id,
                    started_at=self.started_at,
                    task_kind=task_kind,
                    task_id=task_id,
                    completed=completed,
                    error_kind=error_kind,
                    stopped=stopped,
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - liveness must never stop the worker
            log.warning("heartbeat_failed", worker_id=self.worker_id, error=type(exc).__name__)
            return False
        self._last_write = now
        return True


def classify(row: WorkerHeartbeat, *, now: datetime | None = None) -> WorkerView:
    current = now or datetime.now(UTC)
    seen = row.last_seen_at if row.last_seen_at.tzinfo else row.last_seen_at.replace(tzinfo=UTC)
    age = max(0.0, (current - seen).total_seconds())
    if row.stopped_at is not None and row.stopped_at >= seen:
        state = WorkerState.STOPPED
    elif age > STALE_AFTER_S:
        state = WorkerState.STALE
    else:
        state = WorkerState.ALIVE
    return WorkerView(
        worker_id=row.worker_id,
        hostname=row.hostname,
        state=state,
        last_seen_at=seen,
        seconds_since_seen=age,
        current_task_kind=row.current_task_kind if state is WorkerState.ALIVE else None,
        tasks_completed=row.tasks_completed,
        last_error_kind=row.last_error_kind,
    )


async def workers(session: AsyncSession, *, now: datetime | None = None) -> list[WorkerView]:
    """Every worker that has ever beaten, most recently seen first."""
    rows = (
        await session.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_seen_at.desc()))
    ).all()
    return [classify(row, now=now) for row in rows]


__all__ = [
    "HEARTBEAT_INTERVAL_S",
    "STALE_AFTER_S",
    "Heartbeat",
    "WorkerState",
    "WorkerView",
    "classify",
    "record",
    "workers",
]
