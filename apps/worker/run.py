"""Queue consumer.

One task at a time, each in its own transaction. A crash anywhere leaves an
expired lease that the next claim reclaims — nothing is lost and nothing
double-submits, because every handler is idempotent.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog

from apps.worker.apply_job import TaskPayloadError, handle_apply
from apps.worker.crawl_job import CRAWL_TASK_KIND, handle_crawl
from apps.worker.inbox_job import INBOX_TASK_KIND, handle_inbox
from packages.core import db as core_db
from packages.core.config import get_settings
from packages.core.enums import FailureReason
from packages.core.models import Application, QueueTask
from packages.core.queue import (
    DEFAULT_LEASE_SECONDS,
    ClaimedTask,
    claim_task,
    complete_task,
    default_worker_id,
    fail_task,
    renew_lease,
)

log = structlog.get_logger(__name__)

#: How long to wait when the queue is empty before asking again.
IDLE_SLEEP_SECONDS = 1.0

APPLY_TASK_KIND = "apply"

HANDLERS = {
    APPLY_TASK_KIND: handle_apply,
    CRAWL_TASK_KIND: handle_crawl,
    INBOX_TASK_KIND: handle_inbox,
}


async def run_once(
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Claim and run at most one task. Returns True if one was processed."""
    async with core_db.get_sessionmaker()() as session:
        claimed = await claim_task(
            session,
            worker_id=worker_id,
            kinds=list(HANDLERS),
            lease_seconds=lease_seconds,
        )
        if claimed is None:
            return False

        # Commit the claim before doing any work. If the handler raises, the
        # rollback must not also undo the lease and the attempts increment —
        # that would leave the task retryable forever with attempts stuck at
        # zero.
        await session.commit()

        # A browser run can outlast the lease. Without renewal the task would
        # be reclaimed mid-flight and a second worker would start driving the
        # same form — the one way this design could double-submit.
        async with _keep_lease_alive(claimed.task.id, worker_id, lease_seconds):
            await _process(session, claimed)
        return True


@asynccontextmanager
async def _keep_lease_alive(
    task_id: uuid.UUID, worker_id: str, lease_seconds: int
) -> AsyncIterator[None]:
    """Renew the lease on its own session for as long as the body runs."""
    stop = asyncio.Event()

    async def _renew() -> None:
        # A third of the lease gives two chances to renew before it lapses.
        interval = max(lease_seconds / 3, 1.0)
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
            if stop.is_set():
                return
            try:
                async with core_db.get_sessionmaker()() as renewal_session:
                    still_ours = await renew_lease(
                        renewal_session, task_id, worker_id, lease_seconds=lease_seconds
                    )
                if not still_ours:
                    # Someone else owns it now; stop renewing and let the
                    # handler finish. Its writes will be the loser of any race.
                    log.warning("lease_lost", task_id=str(task_id), worker_id=worker_id)
                    return
            except Exception as exc:  # noqa: BLE001 - renewal must not kill the run
                log.warning("lease_renewal_failed", error=type(exc).__name__)

    renewer = asyncio.create_task(_renew())
    try:
        yield
    finally:
        stop.set()
        renewer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await renewer


async def _process(session, claimed: ClaimedTask) -> None:
    # Captured up front: after a rollback the instance is expired, and touching
    # it would trigger a lazy load outside the async context.
    task_id = claimed.task.id
    task_kind = claimed.task.kind

    handler = HANDLERS.get(task_kind)
    if handler is None:
        await _fail(session, task_id, f"no handler for kind {task_kind}", max_attempts=0)
        return

    try:
        await handler(session, claimed)
    except TaskPayloadError as exc:
        # A malformed payload will never succeed, so do not burn retries on it.
        await session.rollback()
        await _fail(session, task_id, str(exc), max_attempts=0)
        log.error("task_payload_invalid", task_id=str(task_id), error=str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - the loop must survive any handler
        await session.rollback()
        # Never log the exception body verbatim: it can carry page content.
        message = f"{type(exc).__name__}: {exc}"
        will_retry = await _fail(session, task_id, message)
        if not will_retry:
            await _mark_application_failed(session, claimed, message)
            await session.commit()
        log.error(
            "task_failed",
            task_id=str(task_id),
            kind=task_kind,
            will_retry=will_retry,
            error=type(exc).__name__,
        )
        return

    await complete_task(session, claimed.task)
    await session.commit()
    log.info("task_done", task_id=str(task_id), kind=task_kind)


async def _fail(session, task_id, message: str, *, max_attempts: int | None = None) -> bool:
    """Re-read the task after a rollback and record the failure."""
    task = await session.get(QueueTask, task_id)
    if task is None:  # pragma: no cover - the claim committed, so it exists
        return False
    kwargs = {} if max_attempts is None else {"max_attempts": max_attempts}
    will_retry = await fail_task(session, task, message, **kwargs)
    await session.commit()
    return will_retry


async def _mark_application_failed(session, claimed: ClaimedTask, message: str) -> None:
    """Out of retries — the application must not sit in `running` forever."""
    from apps.worker.apply_job import park_failed

    raw_id = claimed.task.payload_json.get("application_id")
    if not raw_id:
        return
    application = await session.get(Application, raw_id)
    if application is None:
        return
    await park_failed(session, application, FailureReason.SITE_ERROR, message)


async def run_forever(
    *,
    worker_id: str | None = None,
    lease_seconds: int | None = None,
) -> None:
    settings = get_settings()
    # A configured WORKER_ID is what lets a restarted worker recognize the
    # lease it left behind; the hostname is only a fallback.
    wid = worker_id or settings.worker_id or default_worker_id()
    lease_seconds = lease_seconds if lease_seconds is not None else settings.lease_seconds
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log.info("worker_started", worker_id=wid, lease_seconds=lease_seconds)

    while not stop.is_set():
        try:
            did_work = await run_once(worker_id=wid, lease_seconds=lease_seconds)
        except Exception as exc:  # noqa: BLE001 - a bad claim must not kill the loop
            log.error("claim_failed", error=type(exc).__name__)
            did_work = False

        if not did_work:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=IDLE_SLEEP_SECONDS)

    log.info("worker_stopped", worker_id=wid)


def main() -> None:
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
