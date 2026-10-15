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
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.enums import QueueTaskStatus
from packages.core.models import QueueTask

#: How long a claim stays valid without a heartbeat.
DEFAULT_LEASE_SECONDS = 300

#: Give up after this many attempts and park the task as failed.
DEFAULT_MAX_ATTEMPTS = 3


def db_now_plus(seconds: float) -> Any:
    """`seconds` from now, measured by the *database* clock.

    Every deadline this module writes is later judged by a claim, and a claim
    judges with `clock_timestamp()`. Writing one from the host clock instead
    mixes two clocks: the deadline is then wrong by however far the two have
    drifted apart. That is microseconds on a healthy machine and seconds on a
    container whose clock has slipped, and it breaks in both directions — a
    retry that is not yet due when it should be, or a lease judged expired
    while its owner is still working. Take every deadline from one clock.
    """
    return text("clock_timestamp() + make_interval(secs => :interval_secs)").bindparams(
        interval_secs=seconds
    )


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
    #: Immutable owner and generation for compare-and-set writes.
    task_id: uuid.UUID | None = None
    worker_id: str | None = None
    attempt: int | None = None

    @property
    def reclaimed_from_self(self) -> bool:
        """This worker's own crashed run, replaying.

        Safe to resume: nothing else ever held the lease.
        """
        return self.reclaimed and self.previous_owner == self.worker_id


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
    after_others: list[str] | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> ClaimedTask | None:
    """Atomically claim one runnable task, or return None if there are none.

    Runnable means pending and due, or running with an expired lease. The
    inner SELECT takes a row lock with SKIP LOCKED so concurrent workers step
    over each other's in-flight rows instead of blocking.

    Oldest `run_after` first, except that a kind in `after_others` is taken
    only when nothing else is runnable. Time alone let one tick's 3,234
    discovery tasks go ahead of the 682 board polls queued with them: no
    posting would have arrived for three hours, and an application approved
    in that time would have waited behind them as well. Only runnable work is
    yielded to, so a task scheduled for later holds nothing back.
    """
    kind_filter = ""
    params: dict[str, Any] = {
        "worker_id": worker_id,
        "lease_seconds": lease_seconds,
        "max_attempts": max_attempts,
        "after_others": after_others or [],
    }
    if kinds:
        kind_filter = "AND kind = ANY(:kinds)"
        params["kinds"] = kinds

    exhausted = (
        update(QueueTask)
        .where(
            (
                (
                    (QueueTask.status == QueueTaskStatus.RUNNING.value)
                    & QueueTask.lease_expires_at.is_not(None)
                    & (QueueTask.lease_expires_at < text("clock_timestamp()"))
                )
                | (
                    (QueueTask.status == QueueTaskStatus.PENDING.value)
                    & (QueueTask.run_after <= text("clock_timestamp()"))
                )
            ),
            QueueTask.attempts >= max_attempts,
        )
        .values(
            status=QueueTaskStatus.FAILED.value,
            locked_by=None,
            lease_expires_at=None,
            last_error="lease_expired_retry_limit",
        )
    )
    if kinds:
        exhausted = exhausted.where(QueueTask.kind.in_(kinds))
    await session.execute(exhausted)

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
            AND attempts < :max_attempts
            {kind_filter}
            ORDER BY (kind = ANY(:after_others)), run_after
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
        task_id=task.id,
        worker_id=worker_id,
        attempt=task.attempts,
    )


async def heartbeat(
    session: AsyncSession,
    claimed: ClaimedTask,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Extend the lease in the caller's transaction.

    Note this is only visible to other workers once the caller commits. For
    renewal *during* a long-running handler, use `renew_lease()` on a separate
    session — an uncommitted extension does not stop a reclaim.
    """
    return await _update_claim(
        session,
        claimed,
        lease_expires_at=db_now_plus(lease_seconds),
    )


async def renew_lease(
    session: AsyncSession,
    task_id: uuid.UUID,
    worker_id: str,
    attempt: int,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Extend the lease and commit, so other workers actually see it.

    Returns False if this worker no longer owns the task — meaning the lease
    already lapsed and someone else claimed it.

    The attempt is the claim generation. It prevents a restarted worker with
    the same stable id from renewing an older execution after reclaim.
    """
    result = await session.execute(
        update(QueueTask)
        .where(
            QueueTask.id == task_id,
            QueueTask.locked_by == worker_id,
            QueueTask.attempts == attempt,
            QueueTask.status == QueueTaskStatus.RUNNING.value,
            QueueTask.lease_expires_at.is_not(None),
            QueueTask.lease_expires_at >= text("clock_timestamp()"),
        )
        .values(lease_expires_at=db_now_plus(lease_seconds))
    )
    await session.commit()
    return bool(cast("CursorResult[Any]", result).rowcount)


def _claim_predicates(claimed: ClaimedTask) -> tuple[Any, ...]:
    """SQL predicates identifying exactly one live claim generation."""
    if claimed.task_id is None or claimed.worker_id is None or claimed.attempt is None:
        raise ValueError("claimed task has no ownership token")
    return (
        QueueTask.id == claimed.task_id,
        QueueTask.status == QueueTaskStatus.RUNNING.value,
        QueueTask.locked_by == claimed.worker_id,
        QueueTask.attempts == claimed.attempt,
        QueueTask.lease_expires_at.is_not(None),
        QueueTask.lease_expires_at >= text("clock_timestamp()"),
    )


async def _update_claim(session: AsyncSession, claimed: ClaimedTask, **values: Any) -> bool:
    """Compare-and-set one claim and refresh its ORM view when accepted."""
    await session.flush()
    result = await session.execute(
        update(QueueTask)
        .where(*_claim_predicates(claimed))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    accepted = bool(cast("CursorResult[Any]", result).rowcount)
    if accepted:
        await session.refresh(claimed.task)
    return accepted


async def complete_task(session: AsyncSession, claimed: ClaimedTask) -> bool:
    """Mark done only if this exact claim still owns the task."""
    return await _update_claim(
        session,
        claimed,
        status=QueueTaskStatus.DONE.value,
        locked_by=None,
        lease_expires_at=None,
        last_error=None,
    )


async def fail_task(
    session: AsyncSession,
    claimed: ClaimedTask,
    error: str,
    *,
    retry_in_s: int = 60,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> bool | None:
    """Record a failure. True retries, False terminates, None lost ownership.

    `error` is written to the row, so callers must pass a message that carries
    no secrets, résumé content, or page HTML.
    """
    retrying = claimed.attempt is not None and claimed.attempt < max_attempts
    accepted = await _update_claim(
        session,
        claimed,
        status=(QueueTaskStatus.PENDING.value if retrying else QueueTaskStatus.FAILED.value),
        locked_by=None,
        lease_expires_at=None,
        last_error=error[:2000],
        **({"run_after": db_now_plus(retry_in_s)} if retrying else {}),
    )
    return retrying if accepted else None


async def release_task(session: AsyncSession, claimed: ClaimedTask) -> bool:
    """Hand a task back unfinished, without consuming a retry.

    Used when a task is parked on something external — an approval, an OTP —
    rather than having failed.
    """
    return await _update_claim(
        session,
        claimed,
        status=QueueTaskStatus.PENDING.value,
        locked_by=None,
        lease_expires_at=None,
        attempts=QueueTask.attempts - 1,
    )


class ClaimOwnershipLost(RuntimeError):
    """The task was reclaimed before this execution could write its outcome."""


class TaskDeferred(Exception):  # noqa: N818 - an outcome, not an error
    """Raised by a handler that handed its task back rather than doing the work.

    Needed because returning normally is indistinguishable from success, and
    `run.py::_process` calls `complete_task` on a clean return — which marked a
    freshly deferred task `done` and dropped the work. Caught there and
    **committed**, not rolled back: the deferral is the thing to keep.

    Not a failure: `attempts` is untouched, so being asked to wait never
    exhausts a task's retries.
    """

    def __init__(self, reason: str, *, run_after: datetime) -> None:
        super().__init__(reason)
        self.run_after = run_after


async def defer_task(session: AsyncSession, claimed: ClaimedTask, *, run_after: datetime) -> bool:
    """Put a task back with a later start time, without consuming a retry.

    Distinct from `fail_task`, which increments `attempts` and eventually gives
    up. Being asked to wait by the rate limiter is not a failure and must never
    exhaust a task's retries: a company on a busy host would otherwise be
    dropped for being popular.

    Distinct from `release_task` too, which hands a task back for *now* — that
    is right for an approval, and here it would spin: the worker would reclaim
    the same task immediately and find the host still busy.

    **The claim's attempt is given back.** `claim_task` increments `attempts`
    when it takes the task, which is right in general — a worker that dies
    mid-task must have consumed one, or a crash loop would retry for ever. A
    deferral is neither a crash nor a try: nothing was requested. Leaving the
    increment would spend a task's whole retry budget on being unlucky about
    which host it belongs to, and `DEFAULT_MAX_ATTEMPTS` would then drop a
    perfectly good company for being on a busy board.
    """
    return await _update_claim(
        session,
        claimed,
        status=QueueTaskStatus.PENDING.value,
        locked_by=None,
        locked_at=None,
        lease_expires_at=None,
        run_after=run_after,
        attempts=QueueTask.attempts - 1,
    )


async def pending_count(session: AsyncSession, *, kind: str | None = None) -> int:
    """Count pending tasks, optionally filtered by kind."""
    stmt = select(QueueTask).where(QueueTask.status == QueueTaskStatus.PENDING.value)
    if kind:
        stmt = stmt.where(QueueTask.kind == kind)
    return len((await session.scalars(stmt)).all())


async def expire_leases(session: AsyncSession) -> int:
    """Force every running lease to expire. Test and recovery helper."""
    result = await session.execute(
        update(QueueTask)
        .where(QueueTask.status == QueueTaskStatus.RUNNING.value)
        .values(lease_expires_at=db_now_plus(-1))
    )
    await session.flush()
    return int(cast("CursorResult[Any]", result).rowcount)
