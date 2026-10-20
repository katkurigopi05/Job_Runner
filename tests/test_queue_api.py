"""`/queue`: what the task queue is doing, and withdrawing a task before it runs."""

from __future__ import annotations

from sqlalchemy import update

from packages.core.models import QueueTask
from packages.core.queue import claim_task, enqueue


async def _enqueue(committing_sessionmaker, kind: str, n: int = 1) -> list[str]:
    async with committing_sessionmaker() as session:
        tasks = [await enqueue(session, kind, {"n": i}) for i in range(n)]
        await session.commit()
        return [str(task.id) for task in tasks]


async def test_a_pending_task_is_cancelled(client, committing_sessionmaker) -> None:
    [task_id] = await _enqueue(committing_sessionmaker, "discover_company")

    response = await client.post(f"/queue/tasks/{task_id}/cancel", json={"reason": "duplicate"})

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["last_error"]) == ("cancelled", "cancelled: duplicate")
    assert body["finished_at"] is not None


async def test_a_task_is_cancelled_without_a_body(client, committing_sessionmaker) -> None:
    [task_id] = await _enqueue(committing_sessionmaker, "deep_dive_company")

    response = await client.post(f"/queue/tasks/{task_id}/cancel")

    assert response.status_code == 200
    assert response.json()["last_error"] == "cancelled: cancelled by the owner"


async def test_an_unknown_task_is_not_found(client) -> None:
    response = await client.post(
        "/queue/tasks/00000000-0000-0000-0000-000000000000/cancel", json={}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_a_running_task_cannot_be_cancelled(client, committing_sessionmaker) -> None:
    [task_id] = await _enqueue(committing_sessionmaker, "crawl_company")
    async with committing_sessionmaker() as session:
        await claim_task(session, worker_id="w")
        await session.commit()

    response = await client.post(f"/queue/tasks/{task_id}/cancel", json={})

    assert response.status_code == 409
    assert "running" in response.json()["error"]["message"]


async def test_an_apply_task_is_withdrawn_by_rejecting_its_application(
    client, committing_sessionmaker
) -> None:
    [task_id] = await _enqueue(committing_sessionmaker, "apply")

    response = await client.post(f"/queue/tasks/{task_id}/cancel", json={})

    assert response.status_code == 409
    assert "rejecting the application" in response.json()["error"]["message"]


async def test_the_crawl_tick_cannot_be_cancelled(client, committing_sessionmaker) -> None:
    """It schedules the next one itself: cancelling it would stop crawling."""
    [task_id] = await _enqueue(committing_sessionmaker, "crawl")

    response = await client.post(f"/queue/tasks/{task_id}/cancel", json={})

    assert response.status_code == 409


async def test_every_pending_task_of_a_kind_is_cancelled(client, committing_sessionmaker) -> None:
    await _enqueue(committing_sessionmaker, "discover_company", n=3)
    await _enqueue(committing_sessionmaker, "crawl_company")

    response = await client.post(
        "/queue/cancel", json={"kind": "discover_company", "reason": "too many"}
    )

    assert response.status_code == 200
    assert response.json() == {"kind": "discover_company", "cancelled": 3}
    listed = (await client.get("/queue/tasks", params={"status": "pending"})).json()
    assert [task["kind"] for task in listed] == ["crawl_company"]


async def test_a_kind_that_must_not_be_cancelled_is_refused_whole(client) -> None:
    response = await client.post("/queue/cancel", json={"kind": "inbox"})

    assert response.status_code == 409


async def test_the_stats_report_each_kind(client, committing_sessionmaker) -> None:
    await _enqueue(committing_sessionmaker, "discover_company", n=2)
    [failed] = await _enqueue(committing_sessionmaker, "crawl_company")
    async with committing_sessionmaker() as session:
        await session.execute(
            update(QueueTask)
            .where(QueueTask.id == failed)
            .values(status="failed", finished_at=QueueTask.created_at, last_error="HTTP 404")
        )
        await session.commit()

    response = await client.get("/queue/stats", params={"hours": 24})

    assert response.status_code == 200
    by_kind = {row["kind"]: row for row in response.json()}
    assert by_kind["discover_company"]["pending"] == 2
    assert (by_kind["crawl_company"]["failed"], by_kind["crawl_company"]["last_error"]) == (
        1,
        "HTTP 404",
    )


async def test_the_task_list_filters_and_is_bounded(client, committing_sessionmaker) -> None:
    await _enqueue(committing_sessionmaker, "discover_company", n=3)

    listed = (
        await client.get("/queue/tasks", params={"kind": "discover_company", "limit": 2})
    ).json()

    assert len(listed) == 2
    assert {"id", "kind", "status", "attempts", "run_after", "finished_at"} <= set(listed[0])
    too_many = await client.get("/queue/tasks", params={"limit": 501})
    assert too_many.json()["error"]["code"] == "invalid_request"
