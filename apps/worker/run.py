"""Queue consumer.

One task at a time, each in its own transaction. A crash anywhere leaves an
expired lease that the next claim reclaims — nothing is lost and nothing
double-submits, because every handler is idempotent.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.worker.apply_job import TaskPayloadError, handle_apply
from apps.worker.crawl_company_job import CRAWL_COMPANY_TASK_KIND, handle_crawl_company
from apps.worker.crawl_job import CRAWL_TASK_KIND, handle_crawl
from apps.worker.deep_dive_job import DEEP_DIVE_TASK_KIND, handle_deep_dive_company
from apps.worker.discover_company_job import (
    DISCOVER_COMPANY_TASK_KIND,
    handle_discover_company,
)
from apps.worker.discover_job import DISCOVER_TASK_KIND, handle_discover
from apps.worker.inbox_job import INBOX_TASK_KIND, ensure_inbox_poll, handle_inbox
from packages.core import db as core_db
from packages.core.config import get_settings
from packages.core.enums import FailureReason
from packages.core.heartbeat import Heartbeat
from packages.core.models import Application
from packages.core.queue import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_RETRY_POLICY,
    ClaimedTask,
    ClaimOwnershipLost,
    RetryPolicy,
    TaskDeferred,
    claim_task,
    complete_task,
    default_worker_id,
    fail_task,
    prune_finished,
    renew_lease,
)
from packages.core.trace import correlated, normalize_trace_id

log = structlog.get_logger(__name__)

#: How long to wait when the queue is empty before asking again.
IDLE_SLEEP_SECONDS = 1.0

#: The durable task carries the real mailbox cadence. This is only a repair
#: pass for startup and exhausted retries, so once a minute is enough.
INBOX_SCHEDULE_TICK_SECONDS = 60.0

APPLY_TASK_KIND = "apply"

HANDLERS = {
    APPLY_TASK_KIND: handle_apply,
    CRAWL_TASK_KIND: handle_crawl,
    CRAWL_COMPANY_TASK_KIND: handle_crawl_company,
    DISCOVER_COMPANY_TASK_KIND: handle_discover_company,
    DEEP_DIVE_TASK_KIND: handle_deep_dive_company,
    DISCOVER_TASK_KIND: handle_discover,
    INBOX_TASK_KIND: handle_inbox,
}

#: How a failed task of each kind is retried (`queue.RetryPolicy`): the first
#: wait, doubling each time to the cap. A board or an employer's site that
#: failed is given time to recover; the crawl tick and the inbox poll keep a
#: loop going, so theirs stay short.
RETRY_POLICIES: dict[str, RetryPolicy] = {
    APPLY_TASK_KIND: RetryPolicy(base_s=300, max_delay_s=1800),
    CRAWL_COMPANY_TASK_KIND: RetryPolicy(base_s=120, max_delay_s=1800),
    DISCOVER_COMPANY_TASK_KIND: RetryPolicy(base_s=600, max_delay_s=6 * 3600),
    DEEP_DIVE_TASK_KIND: RetryPolicy(base_s=600, max_delay_s=6 * 3600),
    DISCOVER_TASK_KIND: RetryPolicy(base_s=600, max_delay_s=6 * 3600),
    CRAWL_TASK_KIND: RetryPolicy(base_s=60, max_delay_s=600),
    INBOX_TASK_KIND: RetryPolicy(base_s=60, max_delay_s=600),
}


def retry_policy(kind: str) -> RetryPolicy:
    return RETRY_POLICIES.get(kind, DEFAULT_RETRY_POLICY)


#: The longest one attempt of each kind may run, in seconds, before it is
#: stopped and failed like any other error (and retried, if it has attempts
#: left). Set well above the slowest honest run: this is for a handler that
#: hangs, which renewed its lease for ever and held its worker with it.
#: Every request already has a network timeout; this is the backstop for
#: whatever does not.
TIME_LIMITS: dict[str, float] = {
    APPLY_TASK_KIND: 3600,
    CRAWL_TASK_KIND: 1800,
    CRAWL_COMPANY_TASK_KIND: 1800,
    DISCOVER_COMPANY_TASK_KIND: 1800,
    DEEP_DIVE_TASK_KIND: 3600,
    DISCOVER_TASK_KIND: 3600,
    INBOX_TASK_KIND: 900,
}
DEFAULT_TIME_LIMIT_S = 3600.0


def time_limit(kind: str) -> float:
    return float(TIME_LIMITS.get(kind, DEFAULT_TIME_LIMIT_S))


#: Taken only when nothing else is runnable (`claim_task`), each after the ones
#: before it. Discovery looks for boards nobody has confirmed yet, thousands of
#: companies at a time and about a minute each. Polling the boards already
#: known, an approved application and the inbox are all owed sooner than that.
#: The deep dive looks again at the companies those could not read, and comes
#: last: the owner asked for it once everything else is done (2026-10-19).
YIELDING_KINDS = (DISCOVER_COMPANY_TASK_KIND, DEEP_DIVE_TASK_KIND)


async def run_once(
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    heartbeat: Heartbeat | None = None,
) -> bool:
    """Claim and run at most one task. Returns True if one was processed."""
    async with core_db.get_sessionmaker()() as session:
        claimed = await claim_task(
            session,
            worker_id=worker_id,
            kinds=list(HANDLERS),
            after_others=list(YIELDING_KINDS),
            lease_seconds=lease_seconds,
        )
        if claimed is None:
            # `claim_task` also parks expired work that exhausted its retry
            # budget. Keep that terminal transition even when no task remains
            # to return to this worker.
            await session.commit()
            return False

        # Commit the claim before doing any work. If the handler raises, the
        # rollback must not also undo the lease and the attempts increment —
        # that would leave the task retryable forever with attempts stuck at
        # zero.
        await session.commit()
        if heartbeat is not None:
            # Forced: "which task is the worker on" is the question the setup
            # page is asked while a long browser run is in progress.
            await heartbeat.pulse(force=True, task_kind=claimed.task.kind, task_id=claimed.task.id)

        # A browser run can outlast the lease. Without renewal the task would
        # be reclaimed mid-flight and a second worker would start driving the
        # same form — the one way this design could double-submit.
        task_id = str(claimed.task.id)
        parent_trace = normalize_trace_id((claimed.task.payload_json or {}).get("_trace_id"))
        with correlated(trace_id=parent_trace or task_id, task_id=task_id):
            try:
                async with _keep_lease_alive(claimed, lease_seconds):
                    await _process(session, claimed)
            except ClaimOwnershipLost:
                await session.rollback()
                log.warning(
                    "claim_ownership_lost",
                    task_id=str(claimed.task_id),
                    worker_id=worker_id,
                    attempt=claimed.attempt,
                )
                if heartbeat is not None:
                    await heartbeat.pulse(force=True, error_kind="ClaimOwnershipLost")
                return True
        if heartbeat is not None:
            await heartbeat.pulse(completed=True)
        return True


@asynccontextmanager
async def _keep_lease_alive(claimed: ClaimedTask, lease_seconds: int) -> AsyncIterator[None]:
    """Renew this claim and cancel its body as soon as ownership is lost."""
    if claimed.task_id is None or claimed.worker_id is None or claimed.attempt is None:
        raise ValueError("claimed task has no ownership token")
    task_id = claimed.task_id
    worker_id = claimed.worker_id
    attempt = claimed.attempt
    stop = asyncio.Event()
    lost = asyncio.Event()
    body = asyncio.current_task()
    if body is None:  # pragma: no cover - an async context always has a task
        raise RuntimeError("lease guard requires an asyncio task")

    def _lose_claim() -> None:
        lost.set()
        body.cancel()

    async def _renew() -> None:
        # A third of the lease gives two chances to renew before it lapses.
        interval = max(lease_seconds / 3, 0.1)
        deadline = asyncio.get_running_loop().time() + lease_seconds
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
            if stop.is_set():
                return
            try:
                async with core_db.get_sessionmaker()() as renewal_session:
                    still_ours = await renew_lease(
                        renewal_session,
                        task_id,
                        worker_id,
                        attempt,
                        lease_seconds=lease_seconds,
                    )
                if not still_ours:
                    log.warning(
                        "lease_lost",
                        task_id=str(task_id),
                        worker_id=worker_id,
                        attempt=attempt,
                    )
                    _lose_claim()
                    return
                deadline = asyncio.get_running_loop().time() + lease_seconds
            except Exception as exc:  # noqa: BLE001 - renewal must not kill the run
                log.warning("lease_renewal_failed", error=type(exc).__name__)
                if asyncio.get_running_loop().time() >= deadline:
                    _lose_claim()
                    return

    renewer = asyncio.create_task(_renew())
    try:
        yield
        if lost.is_set():
            raise ClaimOwnershipLost(str(task_id))
    except asyncio.CancelledError as exc:
        if lost.is_set():
            raise ClaimOwnershipLost(str(task_id)) from exc
        raise
    finally:
        stop.set()
        renewer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await renewer


async def _process(session: AsyncSession, claimed: ClaimedTask) -> None:
    # Captured up front: after a rollback the instance is expired, and touching
    # it would trigger a lazy load outside the async context.
    task_id = claimed.task.id
    task_kind = claimed.task.kind

    handler = HANDLERS.get(task_kind)
    if handler is None:
        outcome = await _fail(
            session,
            claimed,
            f"no_handler:{task_kind}",
            max_attempts=0,
        )
        if outcome is None:
            raise ClaimOwnershipLost(str(task_id))
        return

    limit = time_limit(task_kind)
    deadline = asyncio.timeout(limit)
    try:
        async with deadline:
            await handler(session, claimed)
    except ClaimOwnershipLost:
        await session.rollback()
        raise
    except TaskDeferred as exc:
        # Neither done nor failed. The handler handed the task back because its
        # host was busy, *before* making any request, so there is nothing to
        # retry and nothing to complete — and the deferral it wrote has to be
        # kept, which is why this commits where the generic handler rolls back.
        await session.commit()
        log.info(
            "task_deferred",
            task_id=str(task_id),
            kind=task_kind,
            reason=str(exc),
            run_after=exc.run_after.isoformat(),
        )
        return
    except TaskPayloadError as exc:
        # A malformed payload will never succeed, so do not burn retries on it.
        await session.rollback()
        outcome = await _fail(session, claimed, type(exc).__name__, max_attempts=0)
        if outcome is None:
            raise ClaimOwnershipLost(str(task_id)) from exc
        log.error("task_payload_invalid", task_id=str(task_id), error=type(exc).__name__)
        return
    except Exception as exc:  # noqa: BLE001 - the loop must survive any handler
        await session.rollback()
        # Never log the exception body verbatim: it can carry page content.
        # A handler's own TimeoutError is an ordinary failure; only the
        # deadline set here having passed is "timed out".
        timed_out = isinstance(exc, TimeoutError) and deadline.expired()
        message = f"timed out after {limit:g}s" if timed_out else type(exc).__name__
        will_retry = await _fail(session, claimed, message, policy=retry_policy(task_kind))
        if will_retry is None:
            raise ClaimOwnershipLost(str(task_id)) from exc
        if not will_retry:
            await _mark_application_failed(session, claimed, message)
            await session.commit()
            await _notify(session, claimed)
        log.error(
            "task_failed",
            task_id=str(task_id),
            kind=task_kind,
            will_retry=will_retry,
            error=message,
        )
        return

    if not await complete_task(session, claimed):
        await session.rollback()
        raise ClaimOwnershipLost(str(task_id))
    await session.commit()
    log.info("task_done", task_id=str(task_id), kind=task_kind)
    # After the commit, never before: `notify_if_parked` says why.
    await _notify(session, claimed)


async def _notify(session: AsyncSession, claimed: ClaimedTask) -> None:
    """Ring the doorbell if the task left an application waiting on the owner.

    Wrapped whole: the task has already committed and succeeded by the time
    this runs, and a notification problem must not turn that into a failure.
    """
    payload = claimed.task.payload_json or {}
    raw = payload.get("application_id")
    if not raw:
        return
    try:
        from packages.core.notify import notify_if_parked

        await notify_if_parked(session, uuid.UUID(str(raw)))
    except Exception as exc:  # noqa: BLE001 — the work is already done
        log.warning("notify_failed", error=type(exc).__name__)


async def _fail(
    session: AsyncSession,
    claimed: ClaimedTask,
    message: str,
    *,
    max_attempts: int | None = None,
    policy: RetryPolicy | None = None,
) -> bool | None:
    """Record failure only while this exact claim generation is current."""
    will_retry = await fail_task(
        session, claimed, message, policy=policy, max_attempts=max_attempts
    )
    if will_retry is None:
        await session.rollback()
    else:
        await session.commit()
    return will_retry


async def _mark_application_failed(
    session: AsyncSession, claimed: ClaimedTask, message: str
) -> None:
    """Out of retries — the application must not sit in `running` forever."""
    from apps.worker.apply_job import park_failed

    raw_id = claimed.task.payload_json.get("application_id")
    if not raw_id:
        return
    application = await session.get(Application, raw_id)
    if application is None:
        return
    await park_failed(session, application, FailureReason.SITE_ERROR, message)


def _stop_on_signals() -> asyncio.Event:
    """One event that SIGINT and SIGTERM set, registered once per event loop.

    Once, because `add_signal_handler` replaces whatever was registered for
    that signal before. When each pool member registered its own, only the
    last heard the signal: stopping a four-worker pool stopped one of them,
    and the rest could only be killed.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    return stop


async def run_forever(
    *,
    worker_id: str | None = None,
    lease_seconds: int | None = None,
    stop: asyncio.Event | None = None,
    schedule_inbox: bool = True,
) -> None:
    settings = get_settings()
    # A configured WORKER_ID is what lets a restarted worker recognize the
    # lease it left behind; the hostname is only a fallback.
    wid = worker_id or settings.worker_id or default_worker_id()
    lease_seconds = lease_seconds if lease_seconds is not None else settings.lease_seconds
    # A pool hands every member the same event; a lone worker makes its own.
    stop = stop if stop is not None else _stop_on_signals()

    log.info("worker_started", worker_id=wid, lease_seconds=lease_seconds)
    heartbeat = Heartbeat(wid, core_db.get_sessionmaker())
    await heartbeat.pulse(force=True)
    last_reminder_tick = 0.0
    inbox_scheduler = (
        asyncio.create_task(_maintain_inbox_schedule(stop)) if schedule_inbox else None
    )
    # One pruner per pool, beside the inbox scheduler: it runs whether or not
    # the workers are busy, which the idle-time reminders do not.
    queue_pruner = asyncio.create_task(_prune_queue(stop)) if schedule_inbox else None

    try:
        while not stop.is_set():
            try:
                did_work = await run_once(
                    worker_id=wid, lease_seconds=lease_seconds, heartbeat=heartbeat
                )
            except Exception as exc:  # noqa: BLE001 - a bad claim must not kill the loop
                log.error("claim_failed", error=type(exc).__name__)
                await heartbeat.pulse(error_kind=type(exc).__name__)
                did_work = False

            if not did_work:
                # Throttled inside: an idle loop turns every second and writes
                # every HEARTBEAT_INTERVAL_S.
                await heartbeat.pulse()
                last_reminder_tick = await _ring_reminders(last_reminder_tick)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=IDLE_SLEEP_SECONDS)
    finally:
        for background in (inbox_scheduler, queue_pruner):
            if background is not None:
                background.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await background

    # A goodbye, so the setup page can tell a clean stop from a crash.
    await heartbeat.pulse(force=True, stopped=True)
    log.info("worker_stopped", worker_id=wid)


async def _maintain_inbox_schedule(stop: asyncio.Event) -> None:
    """Seed and repair the durable inbox loop without touching mail itself."""
    while not stop.is_set():
        try:
            async with core_db.get_sessionmaker()() as session:
                queued = await ensure_inbox_poll(session)
                await session.commit()
            if queued is not None:
                log.info("inbox_poll_scheduled", task_id=str(queued.id))
        except Exception as exc:  # noqa: BLE001 - email must not stop other work
            log.warning("inbox_schedule_failed", error=type(exc).__name__)

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=INBOX_SCHEDULE_TICK_SECONDS)


#: How often finished tasks are cleared away (`queue.prune_finished`).
PRUNE_QUEUE_EVERY_SECONDS = 3600.0
#: Any number: the same key in every process, so two pools never prune at once.
_PRUNE_LOCK_KEY = 7_204_311_005


async def prune_queue_once() -> int:
    """Clear finished tasks older than the settings keep. Never raises.

    Takes a transaction lock first and skips if another pool holds it. Deletes
    in batches until one removes nothing, then commits. The rows it locks are
    finished ones, which no claim reads, so the workers are not held up.
    """
    settings = get_settings()
    removed = 0
    try:
        async with core_db.get_sessionmaker()() as session:
            locked = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _PRUNE_LOCK_KEY}
            )
            if not locked:
                return 0
            while True:
                gone = await prune_finished(
                    session,
                    keep_done=timedelta(days=settings.queue_keep_done_days),
                    keep_failed=timedelta(days=settings.queue_keep_failed_days),
                )
                removed += gone
                if gone == 0:
                    break
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - housekeeping must not stop the worker
        log.warning("queue_prune_failed", error=type(exc).__name__)
        return removed
    if removed:
        log.info("queue_pruned", removed=removed)
    return removed


async def _prune_queue(stop: asyncio.Event) -> None:
    while not stop.is_set():
        await prune_queue_once()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=PRUNE_QUEUE_EVERY_SECONDS)


#: How often an idle worker checks for task reminders that are due.
REMINDER_TICK_SECONDS = 60.0


async def _ring_reminders(last_tick: float) -> float:
    """Ring due task reminders at most once a minute. Never raises.

    Local notifications through the owner's configured backends only — a
    reminder tells the owner, it never contacts an employer. `ring_due` locks
    with SKIP LOCKED and stamps before delivering, so a pool of workers rings
    each reminder once.
    """
    import time

    now = time.monotonic()
    if last_tick and now - last_tick < REMINDER_TICK_SECONDS:
        return last_tick
    try:
        from packages.tracking.reminders import ring_due

        async with core_db.get_sessionmaker()() as session:
            await ring_due(session)
    except Exception as exc:  # noqa: BLE001 - reminders must not stop the worker
        log.warning("reminder_tick_failed", error=type(exc).__name__)
    return now


async def run_pool(count: int, *, lease_seconds: int | None = None) -> None:
    """Run `count` workers in one process, each claiming independently.

    The queue was always safe for this — `FOR UPDATE SKIP LOCKED` means two
    workers never claim the same row — but nothing could actually start more
    than one, so the safety went unused.

    Each gets a distinct worker id, because the id is what lets a restarted
    worker recognize its own expired lease. Sharing one id across a pool
    would let a worker resume a task another member is still running.

    Concurrency here is for *waiting*, not for CPU: a crawl spends its cycle
    inside the rate limiter and an apply spends it inside a browser, and one
    worker blocked on either is a worker not doing the other. Past a handful
    the limits stop being ours — the rate limiter serializes per host no
    matter how many claimants there are.
    """
    settings = get_settings()
    base = settings.worker_id or default_worker_id()
    stop = _stop_on_signals()

    await asyncio.gather(
        *(
            run_forever(
                worker_id=f"{base}-{index}",
                lease_seconds=lease_seconds,
                stop=stop,
                schedule_inbox=index == 0,
            )
            for index in range(count)
        )
    )


#: A claimant holds one database connection for as long as its task runs,
#: which is a minute at a time while it waits on a host, and takes a second
#: for what runs beside the task: the rate limiter's reservation, the request
#: meter, a heartbeat.
CONNECTIONS_PER_CLAIMANT = 2


def pool_shortfall(workers: int, *, pool_size: int, max_overflow: int) -> str | None:
    """Why this many claimants cannot start on this connection pool, or None.

    Checked before anything is claimed. Past the pool's limit the claimants
    hold every connection and then each waits on the others for one more,
    thirty seconds at a time.
    """
    needed = workers * CONNECTIONS_PER_CLAIMANT
    capacity = pool_size + max_overflow
    if needed <= capacity:
        return None
    return (
        f"--workers {workers} needs {needed} database connections and the pool "
        f"allows {capacity}: set DB_POOL_SIZE={needed} (`make workers n={workers}` does)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Jobrunner queue worker")
    parser.add_argument(
        "-n",
        "--workers",
        type=int,
        default=1,
        help="how many workers to run in this process (default 1)",
    )
    args = parser.parse_args()

    if args.workers < 1:
        parser.error("--workers must be at least 1")
    settings = get_settings()
    refusal = pool_shortfall(
        args.workers, pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow
    )
    if refusal is not None:
        parser.error(refusal)
    if args.workers == 1:
        asyncio.run(run_forever())
    else:
        asyncio.run(run_pool(args.workers))


if __name__ == "__main__":
    main()
