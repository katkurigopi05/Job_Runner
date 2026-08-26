"""Whether the crawler is working, answerable from the dashboard.

The queue was invisible from every screen. `make crawl` enqueues and
`make worker` drains, and nothing said whether either was happening — so a match
feed six days stale looked exactly like a fresh one with nothing new. That is
how an empty "posted in the last day" search reads as "the market is quiet"
rather than "nothing has been looked for".
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from apps.worker.crawl_job import CRAWL_TASK_KIND
from packages.core.enums import QueueTaskStatus
from packages.core.queue import enqueue


async def test_nothing_queued_reads_as_idle(client: AsyncClient) -> None:
    status = (await client.get("/crawl/status")).json()

    assert status["running"] is False
    assert status["pending"] == 0
    assert status["stalled"] is False


@pytest.mark.asyncio
async def test_queued_with_no_worker_is_stalled_not_running(
    client: AsyncClient, worker_session
) -> None:
    """The distinction the indicator exists for.

    A crawl waiting because nobody is draining the queue needs `make worker`. A
    crawl actually in progress needs patience. Conflating them would put a
    reassuring "working" on screen while nothing was happening at all — which is
    the failure this endpoint was written to end, reproduced one level up.
    """
    await enqueue(worker_session, CRAWL_TASK_KIND, {})
    await worker_session.commit()

    status = (await client.get("/crawl/status")).json()

    assert status["pending"] == 1
    assert status["running"] is False
    assert status["stalled"] is True


@pytest.mark.asyncio
async def test_a_claimed_crawl_reads_as_running(client: AsyncClient, worker_session) -> None:
    task = await enqueue(worker_session, CRAWL_TASK_KIND, {})
    task.status = QueueTaskStatus.RUNNING.value
    await worker_session.commit()

    status = (await client.get("/crawl/status")).json()

    assert status["running"] is True
    # Claimed work is not also waiting work — otherwise the indicator would
    # report both states at once and have to pick one arbitrarily.
    assert status["stalled"] is False


@pytest.mark.asyncio
async def test_posting_freshness_is_reported_alongside(client: AsyncClient) -> None:
    """The number that actually answers "are my results current".

    A crawl that ran and found nothing leaves this unchanged, which is the
    truth and is why it is reported separately from the crawl's own status.
    """
    status = (await client.get("/crawl/status")).json()

    assert "newest_posting_at" in status
