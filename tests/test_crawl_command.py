"""Queueing a registry crawl, and the gap that made it necessary.

`apps/worker/crawl_job.py` has been in the worker's handler map since Phase 5.
Nothing ever enqueued it: this project's queue held 70 `apply` tasks and not one
`crawl`, so `handle_crawl` had never run through the worker at all.

The failure that hides behind is the worst kind — postings simply stop being
new, and "no postings since the last sweep" reads exactly like "the sweep never
happened". Nothing errors, no gate fails, and the match feed quietly describes a
job market from whenever someone last ran a crawl by hand.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select, text

from apps.worker.crawl_job import CRAWL_TASK_KIND
from packages.core.enums import QueueTaskStatus
from packages.core.models import QueueTask
from packages.core.queue import enqueue


async def _crawl_count(session, statuses: tuple[str, ...]) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(QueueTask)
        .where(QueueTask.kind == CRAWL_TASK_KIND, QueueTask.status.in_(statuses))
    )


@pytest.mark.asyncio
async def test_the_worker_can_actually_handle_a_crawl_task(db_session) -> None:
    """The kind the script queues has to be the kind the worker dispatches.

    Two constants in two files. A crawl queued under a name the handler map does
    not know would sit `pending` forever while `make crawl` reported success.
    """
    from apps.worker.run import HANDLERS

    assert CRAWL_TASK_KIND in HANDLERS


@pytest.mark.asyncio
async def test_a_second_crawl_is_not_queued_while_one_waits(db_session) -> None:
    """Two crawls minutes apart poll the same hosts and the later emits nothing.

    Worth refusing rather than allowing: a queue filling with redundant crawls
    would spend the per-host rate limit that §2.6 exists to protect, to produce
    nothing.
    """
    unfinished = (QueueTaskStatus.PENDING.value, QueueTaskStatus.RUNNING.value)
    assert await _crawl_count(db_session, unfinished) == 0

    await enqueue(db_session, CRAWL_TASK_KIND, {})
    await db_session.flush()

    assert await _crawl_count(db_session, unfinished) == 1


@pytest.mark.asyncio
async def test_a_finished_crawl_does_not_block_the_next_one(db_session) -> None:
    """The guard is about crawls still to run, not crawls that have run.

    Keyed on `done` being excluded — a guard that counted every crawl ever
    queued would refuse the second sweep of the project's life and every one
    after it.
    """
    unfinished = (QueueTaskStatus.PENDING.value, QueueTaskStatus.RUNNING.value)

    task = await enqueue(db_session, CRAWL_TASK_KIND, {})
    task.status = QueueTaskStatus.DONE.value
    await db_session.flush()

    assert await _crawl_count(db_session, unfinished) == 0


@pytest.mark.asyncio
async def test_requesting_a_crawl_queues_one_and_names_who_asked(db_session) -> None:
    """`request_crawl` is the one door: `make crawl` and the assistant both use it."""
    from apps.worker.crawl_job import request_crawl

    first = await request_crawl(db_session, trigger="manual")
    await db_session.flush()

    assert first.queued is not None
    assert first.waiting == 0
    assert first.queued.payload_json["trigger"] == "manual"


@pytest.mark.asyncio
async def test_a_request_while_a_crawl_waits_queues_nothing(db_session) -> None:
    """The guard itself. The test above it only ever counted rows it added, so it
    passed with the guard deleted; the guard lived in the script's `main()`."""
    from apps.worker.crawl_job import request_crawl

    unfinished = (QueueTaskStatus.PENDING.value, QueueTaskStatus.RUNNING.value)
    await request_crawl(db_session)
    await db_session.flush()

    second = await request_crawl(db_session)
    await db_session.flush()

    assert second.queued is None
    assert second.waiting == 1
    assert await _crawl_count(db_session, unfinished) == 1


#: A lock somebody in this database is waiting for. Scoped to it: the owner's
#: own database is on the same server and may be busy while the suite runs.
_BLOCKED_HERE = text(
    "SELECT count(*) FROM pg_locks WHERE NOT granted AND locktype = 'advisory' "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
)


async def _waiting_on_a_lock_or_done(maker, request: asyncio.Task) -> None:
    """Return once `request` is blocked behind another transaction, or has finished.

    Read from `pg_locks`, not slept for: the second request either stops at a
    lock the first one holds or runs straight through, and which of the two it
    did is the thing under test.
    """
    async with maker() as watcher:
        for _ in range(500):
            if request.done():
                return
            if await watcher.scalar(_BLOCKED_HERE):
                return
            await asyncio.sleep(0.01)
    pytest.fail("the second request neither finished nor waited")


async def test_two_requests_at_once_queue_one_crawl(committing_sessionmaker) -> None:
    """The count and the insert are two statements, and two requests are two
    transactions. `POST /crawl`, the assistant's "run crawler" and `make crawl`
    can all ask in the same moment, and a count cannot see a row another
    transaction has not committed: each found nothing waiting and each queued
    a crawl.

    The second request starts while the first is still uncommitted, which is
    the interleaving that broke. It cannot use `db_session`: that is one
    transaction, and the guard has always held inside one.
    """
    from apps.worker.crawl_job import request_crawl

    unfinished = (QueueTaskStatus.PENDING.value, QueueTaskStatus.RUNNING.value)
    async with committing_sessionmaker() as one, committing_sessionmaker() as two:
        first = await request_crawl(one, trigger="manual")  # queued, not committed
        asking = asyncio.create_task(request_crawl(two, trigger="manual"))
        await _waiting_on_a_lock_or_done(committing_sessionmaker, asking)
        await one.commit()
        second = await asyncio.wait_for(asking, timeout=30)
        await two.commit()

    async with committing_sessionmaker() as reader:
        queued = await _crawl_count(reader, unfinished)
    assert queued == 1, f"{queued} crawls queued by two requests made at once"
    assert first.queued is not None
    assert second.queued is None and second.waiting == 1


async def test_two_sweeps_finishing_at_once_schedule_one_successor(
    committing_sessionmaker,
) -> None:
    """The same count-then-insert, on the sweep that reschedules itself. Its
    guard is what keeps one pending sweep from becoming two, and delivery is
    at-least-once, so two sweeps do finish together."""
    from apps.worker.crawl_job import _schedule_next_tick

    payload = {"dispatch": True}
    async with committing_sessionmaker() as one, committing_sessionmaker() as two:
        await _schedule_next_tick(one, payload, 300)  # queued, not committed
        scheduling = asyncio.create_task(_schedule_next_tick(two, payload, 300))
        await _waiting_on_a_lock_or_done(committing_sessionmaker, scheduling)
        await one.commit()
        await asyncio.wait_for(scheduling, timeout=30)
        await two.commit()

    async with committing_sessionmaker() as reader:
        queued = await _crawl_count(reader, (QueueTaskStatus.PENDING.value,))
    assert queued == 1, f"{queued} sweeps scheduled by two that finished at once"
