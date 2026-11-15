"""Manual scoring is queued once, local-only, and uses the existing scorer."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from apps.worker.rescore_job import RESCORE_TASK_KIND, handle_rescore, request_rescore
from packages.core.enums import QueueTaskStatus
from packages.core.heartbeat import record
from packages.core.models import QueueTask
from packages.core.queue import ClaimedTask


async def test_rescore_queues_one_and_reports_no_worker(client, committing_sessionmaker):
    response = await client.post("/matches/rescore")
    assert response.status_code == 202
    assert response.json() == {"queued": True, "already_waiting": 0, "worker_alive": False}
    assert (await client.post("/matches/rescore")).json()["already_waiting"] == 1
    async with committing_sessionmaker() as session:
        tasks = list((await session.scalars(select(QueueTask))).all())
        assert len(tasks) == 1 and tasks[0].kind == RESCORE_TASK_KIND
        assert set(tasks[0].payload_json) <= {"_trace_id"}
    status = (await client.get("/matches/rescore")).json()
    assert status["pending"] == 1 and not status["running"]
    assert status["last_status"] is None


async def test_rescore_reports_worker_and_finished_task(client, committing_sessionmaker):
    now = datetime.now(UTC)
    async with committing_sessionmaker() as session:
        await record(session, worker_id="test-worker", started_at=now)
        await session.commit()
    assert (await client.post("/matches/rescore")).json()["worker_alive"] is True
    async with committing_sessionmaker() as session:
        task = await session.scalar(select(QueueTask).where(QueueTask.kind == RESCORE_TASK_KIND))
        task.status = QueueTaskStatus.RUNNING.value
        task.lease_expires_at = now + timedelta(minutes=5)
        await session.commit()
    assert (await client.get("/matches/rescore")).json()["running"] is True
    async with committing_sessionmaker() as session:
        task = await session.scalar(select(QueueTask).where(QueueTask.kind == RESCORE_TASK_KIND))
        task.status = QueueTaskStatus.DONE.value
        task.finished_at = now
        await session.commit()
    status = (await client.get("/matches/rescore")).json()
    assert status["pending"] == 0 and status["last_status"] == "done"
    assert (await client.post("/matches/rescore")).json()["queued"] is True


async def test_rescore_is_loopback_only(client):
    from apps.api.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("192.0.2.10", 50000)),
        base_url="http://localhost",
    ) as remote:
        assert (await remote.post("/matches/rescore")).status_code == 401
    assert (await client.get("/matches/rescore")).json()["pending"] == 0


async def test_worker_reuses_rescore_without_committing(db_session, monkeypatch):
    from apps.worker import rescore_job
    from apps.worker.run import HANDLERS, time_limit

    scorer = AsyncMock()
    monkeypatch.setattr(rescore_job, "rescore", scorer)
    task = QueueTask(kind=RESCORE_TASK_KIND, payload_json={})
    await handle_rescore(db_session, ClaimedTask(task=task, reclaimed=False, previous_owner=None))
    scorer.assert_awaited_once_with(db_session)
    assert HANDLERS[RESCORE_TASK_KIND] is handle_rescore
    assert time_limit(RESCORE_TASK_KIND) == 1800


async def test_concurrent_rescore_requests_queue_one(committing_sessionmaker):
    import asyncio

    from tests.test_crawl_command import _waiting_on_a_lock_or_done

    async with committing_sessionmaker() as one, committing_sessionmaker() as two:
        assert await request_rescore(one)
        asking = asyncio.create_task(request_rescore(two))
        await _waiting_on_a_lock_or_done(committing_sessionmaker, asking)
        await one.commit()
        assert not await asyncio.wait_for(asking, timeout=30)
        await two.commit()


async def test_queued_handler_recomputes_matches_idempotently(client, committing_sessionmaker):
    from packages.core.models import Match
    from tests.test_rescore import _owner, _posting

    async with committing_sessionmaker() as session:
        profile, _ = await _owner(session, resume_lines=["Python backend engineer."])
        await _posting(session, title="Backend Engineer", body="Python backend services.")
        profile_id = profile.id
        await session.commit()
    assert (await client.post("/matches/rescore")).status_code == 202
    async with committing_sessionmaker() as session:
        task = await session.scalar(select(QueueTask).where(QueueTask.kind == RESCORE_TASK_KIND))
        claimed = ClaimedTask(task=task, reclaimed=False, previous_owner=None)
        await handle_rescore(session, claimed)
        await handle_rescore(session, claimed)
        rows = list(
            (await session.scalars(select(Match).where(Match.profile_id == profile_id))).all()
        )
        assert len(rows) == 1 and rows[0].score > 0
        await session.rollback()
