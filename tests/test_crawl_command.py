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

import pytest
from sqlalchemy import func, select

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
