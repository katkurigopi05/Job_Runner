"""Postgres-backed job queue with lease-based claims.

Delivery is at-least-once, so every handler must be idempotent. The mechanism
that makes that tractable is the *lease*:

- A claim sets `locked_by` to the worker's stable identity and
  `lease_expires_at` to now + `lease_seconds`.
- Holding an unexpired lease is what makes a worker the exclusive owner of a
  task. No other worker can claim it, because a claim only ever selects rows
  that are pending or whose lease has expired, and the selection runs under
  `FOR UPDATE SKIP LOCKED`.
- A worker that dies mid-task leaves an expired lease. The next claim reclaims
  it and reports `previous_owner`, so the handler can tell its own crashed run
  (`reclaimed_from_self`) from another worker's abandoned one.

That distinction is what stops a retried task from deadlocking: the handler
knows the work is genuinely unowned and can resume it rather than treating the
already-advanced application status as an error.
"""

from __future__ import annotations

import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.enums import QueueTaskStatus
from packages.core.models import QueueTask

#: How long a claim stays valid without a heartbeat.
DEFAULT_LEASE_SECONDS = 300

#: Give up after this many attempts and park the task as failed.
DEFAULT_MAX_ATTEMPTS = 3


def default_worker_id() -> str:
    """Stable across restarts, so a worker can recognize its own dead lease.

    Deliberately *not* pid-based: a crashed worker that comes back with a new
    pid must still identify the lease it left behind as its own.
    """
    return socket.gethostname()


@dataclass(frozen=True)
class ClaimedTask:
    """A task this worker now exclusively owns, until the lease expires."""

    task: QueueTask
    #: True when this claim took over an expired lease rather than a fresh task.
    reclaimed: bool
    #: Who held the expired lease, when reclaimed.
    previous_owner: str | None

    @property
    def reclaimed_from_self(self) -> bool:
        """This worker's own crashed run, replaying.

        Safe to resume: nothing else ever held the lease.
        """
        return self.reclaimed and self.previous_owner == self.task.locked_by


async def enqueue(
    session: AsyncSession,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    run_after: datetime | None = None,
) -> QueueTask:
    """Add a task. Does not commit — the caller owns the transaction."""
    task = QueueTask(
        kind=kind,
        payload_json=payload or {},
        status=QueueTaskStatus.PENDING.value,
    )
    if run_after is not None:
        task.run_after = run_after
    session.add(task)
    await session.flush()
    return task


async def claim_task(
    session: AsyncSession,
    *,
    worker_id: str,
    kinds: list[str] | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> ClaimedTask | None:
    """Atomically claim one runnable task, or return None if there are none.

    Runnable means pending and due, or running with an expired lease. The
    inner SELECT takes a row lock with SKIP LOCKED so concurrent workers step
    over each other's in-flight rows instead of blocking.
    """
    kind_filter = ""
    params: dict[str, Any] = {"worker_id": worker_id, "lease_seconds": lease_seconds}
    if kinds:
        kind_filter = "AND kind = ANY(:kinds)"
        params["kinds"] = kinds

    # Captured before the UPDATE overwrites locked_by, so the caller can tell
    # whose lease was reclaimed.
    stmt = text(f"""
        WITH claimable AS (
            SELECT id, locked_by AS previous_owner, lease_expires_at
            FROM queue_tasks
            WHERE (
                (status = 'pending' AND run_after <= clock_timestamp())
                OR (status = 'running' AND lease_expires_at IS NOT NULL
                    AND lease_expires_at < clock_timestamp())
            )
            {kind_filter}
            ORDER BY run_after
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE queue_tasks AS q
        SET status = 'running',
            locked_at = clock_timestamp(),
            locked_by = :worker_id,
            lease_expires_at = clock_timestamp() + make_interval(secs => :lease_seconds),
            attempts = q.attempts + 1
        FROM claimable c
        WHERE q.id = c.id
        RETURNING q.id, c.previous_owner, c.lease_expires_at IS NOT NULL AS was_running
    """)

    row = (await session.execute(stmt, params)).one_or_none()
    if row is None:
        return None

    task = await session.get(QueueTask, row.id)
    if task is None:  # pragma: no cover - the row was just updated in this txn
        return None
    await session.refresh(task)

    return ClaimedTask(
        task=task,
        reclaimed=bool(row.was_running),
        previous_owner=row.previous_owner,
    )


async def heartbeat(
    session: AsyncSession,
    task: QueueTask,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> None:
    """Extend the lease in the caller's transaction.

    Note this is only visible to other workers once the caller commits. For
    renewal *during* a long-running handler, use `renew_lease()` on a separate
    session — an uncommitted extension does not stop a reclaim.
    """
    task.lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
    await session.flush()


async def renew_lease(
    session: AsyncSession,
    task_id: uuid.UUID,
    worker_id: str,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Extend the lease and commit, so other workers actually see it.

    Returns False if this worker no longer owns the task — meaning the lease
    already lapsed and someone else claimed it. The caller should stop work
    rather than keep driving a form it no longer owns.
    """
    result = await session.execute(
        update(QueueTask)
        .where(
            QueueTask.id == task_id,
            QueueTask.locked_by == worker_id,
            QueueTask.status == QueueTaskStatus.RUNNING.value,
        )
        .values(lease_expires_at=datetime.now(UTC) + timedelta(seconds=lease_seconds))
    )
    await session.commit()
    return bool(cast("CursorResult[Any]", result).rowcount)


async def complete_task(session: AsyncSession, task: QueueTask) -> None:
    """Mark done and release the lease."""
    task.status = QueueTaskStatus.DONE.value
    task.locked_by = None
    task.lease_expires_at = None
    task.last_error = None
    await session.flush()


async def fail_task(
    session: AsyncSession,
    task: QueueTask,
    error: str,
    *,
    retry_in_s: int = 60,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> bool:
    """Record a failure. Returns True if the task will be retried.

    `error` is written to the row, so callers must pass a message that carries
    no secrets, résumé content, or page HTML.
    """
    task.last_error = error[:2000]
    task.locked_by = None
    task.lease_expires_at = None

    if task.attempts >= max_attempts:
        task.status = QueueTaskStatus.FAILED.value
        await session.flush()
        return False

    task.status = QueueTaskStatus.PENDING.value
    task.run_after = datetime.now(UTC) + timedelta(seconds=retry_in_s)
    await session.flush()
    return True


async def release_task(session: AsyncSession, task: QueueTask) -> None:
    """Hand a task back unfinished, without consuming a retry.

    Used when a task is parked on something external — an approval, an OTP —
    rather than having failed.
    """
    task.status = QueueTaskStatus.PENDING.value
    task.locked_by = None
    task.lease_expires_at = None
    await session.flush()


async def pending_count(session: AsyncSession, *, kind: str | None = None) -> int:
    stmt = select(QueueTask).where(QueueTask.status == QueueTaskStatus.PENDING.value)
    if kind:
        stmt = stmt.where(QueueTask.kind == kind)
    return len((await session.scalars(stmt)).all())


async def expire_leases(session: AsyncSession) -> int:
    """Force every running lease to expire. Test and recovery helper."""
    result = await session.execute(
        update(QueueTask)
        .where(QueueTask.status == QueueTaskStatus.RUNNING.value)
        .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.flush()
    return int(cast("CursorResult[Any]", result).rowcount)
