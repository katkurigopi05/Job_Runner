"""How much crawl work is left, and how long it will take, beside the spider.

Asked for by the owner on 2026-10-19, instead of asking each time: the
backlog of board polls, discovery and deep dives, with the recent rate and
an estimate of when each is done.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, update

from packages.core.models import QueueTask
from packages.core.queue import enqueue
from packages.crawler.progress import crawl_backlog


async def _tasks(session, kind: str, payloads: list[dict], **values) -> None:
    for payload in payloads:
        task = await enqueue(session, kind, payload)
        if values:
            await session.execute(update(QueueTask).where(QueueTask.id == task.id).values(**values))


async def _done_recently(session, kind: str, count: int, minutes_ago: float = 10) -> None:
    await _tasks(
        session,
        kind,
        [{"company_id": f"done-{n}"} for n in range(count)],
        status="done",
        finished_at=func.now() - timedelta(minutes=minutes_ago),
    )


async def test_each_stage_counts_what_is_left_and_how_fast_it_goes(db_session) -> None:
    # 4 discovery tasks for 3 companies: one company was queued twice.
    await _tasks(
        db_session,
        "discover_company",
        [{"company_id": c} for c in ("a", "b", "c", "a")],
    )
    await _tasks(db_session, "discover_company", [{"company_id": "d"}], status="running")
    await _done_recently(db_session, "discover_company", 60)
    await _done_recently(db_session, "discover_company", 99, minutes_ago=45)  # outside the window

    stages = {stage.stage: stage for stage in await crawl_backlog(db_session)}

    discovery = stages["discovery"]
    assert (discovery.waiting, discovery.running, discovery.items) == (4, 1, 4)
    assert discovery.per_minute == 2.0  # 60 in 30 minutes
    assert discovery.eta_minutes == 2.5  # 5 tasks at 2 a minute
    assert discovery.after is None


async def test_deep_dives_wait_for_discovery_and_say_so(db_session) -> None:
    await _tasks(db_session, "discover_company", [{"company_id": "a"}])
    await _tasks(db_session, "deep_dive_company", [{"issue_id": i} for i in ("x", "y")])

    stages = {stage.stage: stage for stage in await crawl_backlog(db_session)}

    deep = stages["deep dives"]
    assert (deep.waiting, deep.items, deep.per_minute) == (2, 2, 0.0)
    assert deep.eta_minutes is None
    assert deep.after == "discovery"


async def test_work_scheduled_for_later_is_left_but_not_waiting(db_session) -> None:
    await _tasks(
        db_session,
        "crawl_company",
        [{"company_id": "a"}],
        run_after=func.now() + timedelta(minutes=20),
    )

    [boards] = [s for s in await crawl_backlog(db_session) if s.stage == "boards"]

    assert (boards.waiting, boards.scheduled, boards.items) == (0, 1, 1)


async def test_a_stage_with_nothing_left_still_reports(db_session) -> None:
    stages = {stage.stage: stage for stage in await crawl_backlog(db_session)}

    assert set(stages) == {"boards", "discovery", "deep dives"}
    assert all(s.items == 0 and s.eta_minutes is None for s in stages.values())


async def test_the_status_route_carries_the_backlog(client, committing_sessionmaker) -> None:
    async with committing_sessionmaker() as session:
        await _tasks(session, "discover_company", [{"company_id": "a"}, {"company_id": "b"}])
        await session.commit()

    body = (await client.get("/crawl/status")).json()

    discovery = next(stage for stage in body["backlog"] if stage["stage"] == "discovery")
    assert (discovery["waiting"], discovery["items"]) == (2, 2)
    assert {"per_minute", "eta_minutes", "after", "running", "scheduled"} <= set(discovery)
