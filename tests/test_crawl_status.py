"""Whether the crawler is working, answerable from the dashboard.

The queue was invisible from every screen. `make crawl` enqueues and
`make worker` drains, and nothing said whether either was happening — so a match
feed six days stale looked exactly like a fresh one with nothing new. That is
how an empty "posted in the last day" search reads as "the market is quiet"
rather than "nothing has been looked for".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from apps.worker.crawl_job import CRAWL_TASK_KIND
from packages.core.enums import QueueTaskStatus
from packages.core.heartbeat import record
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


@pytest.mark.parametrize("kind", ["discover_company", "crawl_company"])
async def test_company_work_keeps_the_spider_running(client, worker_session, kind) -> None:
    task = await enqueue(worker_session, kind, {})
    task.status = "running"
    await worker_session.commit()

    status = (await client.get("/crawl/status")).json()
    assert status["running"] is True
    assert status["stalled"] is False


@pytest.mark.parametrize("alive", [False, True])
async def test_a_future_sweep_is_idle_not_stalled(client, worker_session, alive) -> None:
    now = datetime.now(UTC)
    await enqueue(worker_session, "crawl", {"dispatch": True}, run_after=now + timedelta(hours=1))
    if alive:
        await record(worker_session, worker_id="crawler", started_at=now)
    await worker_session.commit()

    status = (await client.get("/crawl/status")).json()
    assert status["running"] is False
    assert status["pending"] == 0
    assert status["stalled"] is False


@pytest.mark.parametrize("kind", ["crawl", "discover_company", "crawl_company"])
async def test_ready_work_with_a_live_worker_keeps_moving(client, worker_session, kind) -> None:
    await enqueue(worker_session, kind, {})
    await record(worker_session, worker_id="crawler", started_at=datetime.now(UTC))
    await worker_session.commit()

    status = (await client.get("/crawl/status")).json()
    assert status["running"] is True
    assert status["pending"] == 1
    assert status["stalled"] is False


async def test_a_delayed_sweep_retry_is_unfinished_work(client, worker_session) -> None:
    now = datetime.now(UTC)
    task = await enqueue(worker_session, "crawl", {}, run_after=now + timedelta(minutes=2))
    task.attempts = 1
    await record(worker_session, worker_id="crawler", started_at=now)
    await worker_session.commit()

    status = (await client.get("/crawl/status")).json()
    assert status["running"] is True
    assert status["pending"] == 1
    assert status["stalled"] is False


@pytest.mark.parametrize("kind", ["discover_company", "crawl_company"])
@pytest.mark.parametrize("worker_state", ["alive", "stale", "stopped", "missing"])
async def test_deferred_company_work_is_not_finished(
    client, worker_session, kind, worker_state
) -> None:
    now = datetime.now(UTC)
    await enqueue(worker_session, kind, {}, run_after=now + timedelta(minutes=2))
    if worker_state != "missing":
        await record(
            worker_session,
            worker_id="crawler",
            started_at=now,
            now=now - timedelta(minutes=5) if worker_state == "stale" else now,
            stopped=worker_state == "stopped",
        )
    await worker_session.commit()

    status = (await client.get("/crawl/status")).json()
    assert status["pending"] == 1
    assert status["running"] is (worker_state == "alive")
    assert status["stalled"] is (worker_state != "alive")


@pytest.mark.parametrize("terminal", ["done", "failed"])
async def test_spider_stays_active_from_dispatch_through_last_company(
    client, worker_session, terminal
) -> None:
    now = datetime.now(UTC)
    await record(worker_session, worker_id="crawler", started_at=now)
    sweep = await enqueue(worker_session, "crawl", {"dispatch": True})
    sweep.status = "running"
    await worker_session.commit()
    assert (await client.get("/crawl/status")).json()["running"] is True

    # The sweep finishes, but discovery is still queued. The next scheduled
    # sweep is not part of this cycle and must not keep it alive afterwards.
    sweep.status = "done"
    discovery = await enqueue(worker_session, "discover_company", {})
    await enqueue(worker_session, "crawl", {"dispatch": True}, run_after=now + timedelta(hours=1))
    await worker_session.commit()
    assert (await client.get("/crawl/status")).json()["running"] is True

    discovery.status = "done"
    company = await enqueue(worker_session, "crawl_company", {})
    other_company = await enqueue(worker_session, "crawl_company", {})
    other_company.status = terminal
    await worker_session.commit()
    assert (await client.get("/crawl/status")).json()["running"] is True

    company.status = "running"
    await worker_session.commit()
    assert (await client.get("/crawl/status")).json()["running"] is True

    company.status = terminal
    await worker_session.commit()
    status = (await client.get("/crawl/status")).json()
    assert status["running"] is False
    assert status["pending"] == 0
    assert status["stalled"] is False


async def test_unrelated_work_does_not_animate_the_spider(client, worker_session) -> None:
    task = await enqueue(worker_session, "apply", {})
    task.status = "running"
    await record(
        worker_session, worker_id="worker", started_at=datetime.now(UTC), task_kind="apply"
    )
    await worker_session.commit()
    status = (await client.get("/crawl/status")).json()
    assert status["running"] is False
    assert status["pending"] == 0
    assert status["stalled"] is False


async def _claimed_by(session, kind: str, *, lease_ends_at: datetime):
    task = await enqueue(session, kind, {})
    task.status = "running"
    task.locked_by = "gone-0"
    task.lease_expires_at = lease_ends_at
    return task


@pytest.mark.parametrize("kind", ["crawl", "discover_company", "crawl_company"])
@pytest.mark.parametrize("worker", ["missing", "stopped"])
async def test_a_dead_workers_claim_is_stalled_not_running(
    client, worker_session, kind, worker
) -> None:
    """A worker killed mid-task leaves its row `running` until a claimant takes
    the expired lease back. With no claimant alive nothing ever does, and
    counting the row as work in progress kept the spider walking for good.
    Found by the 2026-10-14 audit, on this route before and after the
    handoff-aware rewrite above."""
    now = datetime.now(UTC)
    await _claimed_by(worker_session, kind, lease_ends_at=now - timedelta(hours=2))
    if worker == "stopped":
        await record(
            worker_session,
            worker_id="gone-0",
            started_at=now - timedelta(hours=3),
            now=now - timedelta(hours=2),
            stopped=True,
        )
    await worker_session.commit()

    status = (await client.get("/crawl/status")).json()
    assert status["running"] is False
    assert status["stalled"] is True
    assert status["pending"] == 1


async def test_a_dead_workers_claim_with_a_live_worker_keeps_moving(client, worker_session) -> None:
    """A live claimant will take the expired lease back on its next claim."""
    now = datetime.now(UTC)
    await _claimed_by(worker_session, "crawl_company", lease_ends_at=now - timedelta(minutes=10))
    await record(worker_session, worker_id="crawler", started_at=now)
    await worker_session.commit()

    status = (await client.get("/crawl/status")).json()
    assert status["running"] is True
    assert status["stalled"] is False
    assert status["pending"] == 1


async def test_a_claim_whose_lease_is_current_is_running(client, worker_session) -> None:
    """Somebody holds it now, whatever the heartbeat table last heard."""
    now = datetime.now(UTC)
    await _claimed_by(worker_session, "crawl_company", lease_ends_at=now + timedelta(minutes=4))
    await worker_session.commit()

    status = (await client.get("/crawl/status")).json()
    assert status["running"] is True
    assert status["stalled"] is False
    assert status["pending"] == 0


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
