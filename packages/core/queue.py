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
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, and_, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.enums import QueueTaskStatus
from packages.core.models import QueueTask
from packages.core.trace import current_trace_id

#: How long a claim stays valid without a heartbeat.
DEFAULT_LEASE_SECONDS = 300

#: Give up after this many attempts and park the task as failed.
DEFAULT_MAX_ATTEMPTS = 3

#: The wait before a failed task's first retry. Each later retry waits twice
#: as long as the one before, up to `DEFAULT_RETRY_MAX_DELAY_SECONDS`.
DEFAULT_RETRY_BASE_SECONDS = 60.0
DEFAULT_RETRY_MAX_DELAY_SECONDS = 3600.0

#: The statuses a task does not leave.
FINISHED_STATUSES = (
    QueueTaskStatus.DONE.value,
    QueueTaskStatus.FAILED.value,
    QueueTaskStatus.CANCELLED.value,
)

#: The database's clock, which every claim and lease is judged by.
_CLOCK = text("clock_timestamp()")


@dataclass(frozen=True)
class RetryPolicy:
    """How a failed task is retried: how long it waits, and when it stops.

    The wait after attempt n is `base_s * 2**(n - 1)`, capped at `max_delay_s`.
    It was a flat 60 seconds for every kind and every attempt (2026-10-19), so
    a board that was down for an hour was asked again at the same pace each
    time, and a retry had no time to let anything recover.

    `max_attempts` is honoured by `fail_task`. A task whose worker died is
    judged by `claim_task`'s own limit instead, `DEFAULT_MAX_ATTEMPTS`, so a
    kind given more attempts than that loses the extra ones to a crash.
    """

    base_s: float = DEFAULT_RETRY_BASE_SECONDS
    max_delay_s: float = DEFAULT_RETRY_MAX_DELAY_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS

    def delay_after(self, attempt: int) -> float:
        """Seconds to wait after failing on `attempt` (1 for the first try)."""
        doublings = min(max(attempt - 1, 0), 30)
        return float(min(self.base_s * 2**doublings, self.max_delay_s))


DEFAULT_RETRY_POLICY = RetryPolicy()


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
    kept_payload = dict(payload or {})
    if trace_id := current_trace_id():
        # Reserved metadata, carried in the existing JSON envelope so old
        # workers continue to ignore it and no schema cutover is required.
        kept_payload["_trace_id"] = trace_id
    task = QueueTask(
        kind=kind,
        payload_json=kept_payload,
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

    `after_others` is ordered: each kind in it also waits for the kinds
    before it. The deep dive comes after discovery (`issues.py`).
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
            finished_at=_CLOCK,
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
            ORDER BY coalesce(array_position(CAST(:after_others AS varchar[]), kind), 0),
                run_after
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
        # With `locked_at`, which a claim set and nothing here clears, this is
        # how long the last attempt took.
        finished_at=_CLOCK,
    )


async def fail_task(
    session: AsyncSession,
    claimed: ClaimedTask,
    error: str,
    *,
    policy: RetryPolicy | None = None,
    retry_in_s: float | None = None,
    max_attempts: int | None = None,
) -> bool | None:
    """Record a failure. True retries, False terminates, None lost ownership.

    The retry waits as `policy` says for this attempt. `retry_in_s` and
    `max_attempts` override it, for a caller that knows better: a malformed
    payload will never succeed, so it is failed with `max_attempts=0`.

    `error` is written to the row, so callers must pass a message that carries
    no secrets, résumé content, or page HTML.
    """
    rule = policy or DEFAULT_RETRY_POLICY
    limit = rule.max_attempts if max_attempts is None else max_attempts
    attempt = claimed.attempt
    retrying = attempt is not None and attempt < limit
    wait = rule.delay_after(attempt or 1) if retry_in_s is None else retry_in_s
    accepted = await _update_claim(
        session,
        claimed,
        status=(QueueTaskStatus.PENDING.value if retrying else QueueTaskStatus.FAILED.value),
        locked_by=None,
        lease_expires_at=None,
        last_error=error[:2000],
        **({"run_after": db_now_plus(wait)} if retrying else {"finished_at": _CLOCK}),
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


async def cancel_pending(
    session: AsyncSession,
    *,
    reason: str,
    task_id: uuid.UUID | None = None,
    kind: str | None = None,
    refuse_kinds: Collection[str] = (),
) -> list[uuid.UUID]:
    """Cancel tasks that have not started: one by id, or every one of a kind.

    Returns the ids cancelled. Does not commit.

    Only `pending` tasks, retries waiting their turn included. A running task
    is left to finish or fail: stopping a handler partway is what a lost lease
    does, and a run cut short by hand is not a state any handler expects to
    be left in. `refuse_kinds` are kinds whose loss breaks something else,
    which the caller names: an apply task is withdrawn by rejecting its
    application, and the crawl tick and the inbox poll each schedule their
    own next run, so cancelling one would stop them for good.
    """
    if task_id is None and kind is None:
        raise ValueError("name a task or a kind to cancel")
    statement = update(QueueTask).where(QueueTask.status == QueueTaskStatus.PENDING.value)
    if task_id is not None:
        statement = statement.where(QueueTask.id == task_id)
    if kind is not None:
        statement = statement.where(QueueTask.kind == kind)
    if refuse_kinds:
        statement = statement.where(QueueTask.kind.not_in(list(refuse_kinds)))
    result = await session.execute(
        statement.values(
            status=QueueTaskStatus.CANCELLED.value,
            finished_at=_CLOCK,
            last_error=f"cancelled: {reason}"[:2000],
        )
        .returning(QueueTask.id)
        .execution_options(synchronize_session=False)
    )
    return list(result.scalars().all())


async def prune_finished(
    session: AsyncSession,
    *,
    keep_done: timedelta,
    keep_failed: timedelta,
    batch: int = 10_000,
) -> int:
    """Delete finished tasks older than they are kept. Does not commit.

    Returns how many went. Nothing cleared them before 2026-10-19, when
    63,436 finished tasks dated back two months.

    `done` and `cancelled` are kept for `keep_done`, `failed` for the longer
    `keep_failed`: a failure is what someone comes back to look at. The newest
    finished task of each kind is kept whatever its age, because the crawl
    status reports when the last crawl finished and a quiet week must not
    erase that. A task finished before `finished_at` existed is aged by when
    it was created. At most `batch` rows go per call, so one call never holds
    a long lock on the table the workers claim from.
    """
    finished = func.coalesce(QueueTask.finished_at, QueueTask.created_at)
    newest_of_each_kind = (
        select(QueueTask.id)
        .where(QueueTask.status.in_(FINISHED_STATUSES))
        .distinct(QueueTask.kind)
        .order_by(QueueTask.kind, finished.desc())
    )
    old = or_(
        and_(
            QueueTask.status.in_((QueueTaskStatus.DONE.value, QueueTaskStatus.CANCELLED.value)),
            finished < func.now() - keep_done,
        ),
        and_(
            QueueTask.status == QueueTaskStatus.FAILED.value,
            finished < func.now() - keep_failed,
        ),
    )
    doomed = select(QueueTask.id).where(old, QueueTask.id.not_in(newest_of_each_kind)).limit(batch)
    result = await session.execute(
        delete(QueueTask)
        .where(QueueTask.id.in_(doomed))
        .execution_options(synchronize_session=False)
    )
    return int(cast("CursorResult[Any]", result).rowcount)


@dataclass(frozen=True)
class KindStats:
    """What one kind of task is doing, and did in a recent window."""

    kind: str
    pending: int
    running: int
    #: Finished in the window.
    done: int
    failed: int
    cancelled: int
    #: How long a task that finished in the window took, last attempt only.
    median_seconds: float | None
    p95_seconds: float | None
    #: How long the oldest task that could run now has been waiting.
    oldest_waiting_seconds: float | None
    #: The most recent failure in the window.
    last_error: str | None


async def queue_stats(session: AsyncSession, *, since: timedelta) -> list[KindStats]:
    """Counts, durations and the latest failure for each kind of task."""
    window = {"since_s": since.total_seconds()}
    rows = (
        await session.execute(
            text("""
            SELECT kind,
              count(*) FILTER (WHERE status = 'pending') AS pending,
              count(*) FILTER (WHERE status = 'running') AS running,
              count(*) FILTER (WHERE status = 'done' AND finished_at >= cutoff) AS done,
              count(*) FILTER (WHERE status = 'failed' AND finished_at >= cutoff) AS failed,
              count(*) FILTER (WHERE status = 'cancelled' AND finished_at >= cutoff)
                AS cancelled,
              percentile_cont(0.5) WITHIN GROUP (
                ORDER BY extract(epoch FROM finished_at - locked_at))
                FILTER (WHERE status = 'done' AND finished_at >= cutoff
                        AND locked_at IS NOT NULL) AS median_seconds,
              percentile_cont(0.95) WITHIN GROUP (
                ORDER BY extract(epoch FROM finished_at - locked_at))
                FILTER (WHERE status = 'done' AND finished_at >= cutoff
                        AND locked_at IS NOT NULL) AS p95_seconds,
              extract(epoch FROM now() - min(run_after)
                FILTER (WHERE status = 'pending' AND run_after <= now()))
                AS oldest_waiting_seconds
            FROM queue_tasks,
                 LATERAL (SELECT now() - make_interval(secs => :since_s) AS cutoff) w
            WHERE status IN ('pending', 'running') OR finished_at >= cutoff
            GROUP BY kind
            ORDER BY kind
            """),
            window,
        )
    ).all()
    errors = dict(
        (
            await session.execute(
                text("""
                SELECT DISTINCT ON (kind) kind, last_error
                FROM queue_tasks
                WHERE status = 'failed'
                  AND finished_at >= now() - make_interval(secs => :since_s)
                ORDER BY kind, finished_at DESC
                """),
                window,
            )
        )
        .tuples()
        .all()
    )

    def seconds(value: Any) -> float | None:
        return None if value is None else float(value)

    return [
        KindStats(
            kind=row.kind,
            pending=row.pending,
            running=row.running,
            done=row.done,
            failed=row.failed,
            cancelled=row.cancelled,
            median_seconds=seconds(row.median_seconds),
            p95_seconds=seconds(row.p95_seconds),
            oldest_waiting_seconds=seconds(row.oldest_waiting_seconds),
            last_error=errors.get(row.kind),
        )
        for row in rows
    ]


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
