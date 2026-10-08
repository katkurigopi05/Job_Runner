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


# --------------------------------------------------------------------------
# Starting one
# --------------------------------------------------------------------------
#
# `make crawl` and the assistant's "run crawler" both start a crawl, and
# neither is a route. The MCP tools go through HTTP on purpose, so a tool that
# starts one needs somewhere to ask.


@pytest.mark.asyncio
async def test_starting_a_crawl_queues_one_and_says_whether_anything_will_run_it(
    client: AsyncClient,
) -> None:
    started = await client.post("/crawl")

    assert started.status_code == 200, started.text
    assert started.json() == {"queued": True, "already_waiting": 0, "worker_alive": False}
    status = (await client.get("/crawl/status")).json()
    assert status["pending"] == 1 and status["stalled"] is True


@pytest.mark.asyncio
async def test_a_second_start_does_not_queue_a_second_crawl(client: AsyncClient) -> None:
    """`request_crawl`'s guard, reaching the route: two crawls would poll the
    same hosts minutes apart and spend the per-host limit §2.6 protects."""
    await client.post("/crawl")
    again = (await client.post("/crawl")).json()

    assert again["queued"] is False and again["already_waiting"] == 1
    assert (await client.get("/crawl/status")).json()["pending"] == 1


def test_the_route_starts_a_crawl_through_the_one_door() -> None:
    """Not by enqueueing for itself. `request_crawl` is where the
    one-crawl-at-a-time rule lives, and a second way in would not have it."""
    import ast
    import inspect

    from apps.api.routers import crawl

    called = {
        node.func.id
        for node in ast.walk(ast.parse(inspect.getsource(crawl.start_crawl)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "request_crawl" in called and "enqueue" not in called
