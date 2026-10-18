from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid
import pytest
from sqlalchemy import select, update
from apps.worker import run
from packages.core.models import QueueTask
from packages.core.queue import claim_task, complete_task, enqueue, expire_leases


@pytest.mark.asyncio(loop_scope="session")
async def test_expired_claims_stop_at_the_retry_limit(db_session):
    await enqueue(db_session, "audit_fixture", {})
    for index in range(3):
        assert await claim_task(db_session, worker_id=f"worker-{index}") is not None
        await expire_leases(db_session)
    fourth = await claim_task(db_session, worker_id="worker-4")
    assert fourth is None, "A fourth crash-recovery claim bypassed DEFAULT_MAX_ATTEMPTS=3"


@pytest.mark.asyncio(loop_scope="session")
async def test_old_worker_cannot_complete_a_reclaimed_task(db_session):
    await enqueue(db_session, "audit_fixture", {})
    old = await claim_task(db_session, worker_id="worker-old")
    await db_session.execute(update(QueueTask).where(QueueTask.id==old.task.id).values(locked_by="worker-new", attempts=2).execution_options(synchronize_session=False))
    await complete_task(db_session, old.task)
    row = (await db_session.execute(select(QueueTask.status, QueueTask.locked_by).where(QueueTask.id==old.task.id))).one()
    assert tuple(row)==("running", "worker-new"), "Old worker cleared the new owner's lease and marked its task done"


@pytest.mark.asyncio(loop_scope="session")
async def test_failure_persistence_redacts_exception_body(monkeypatch):
    sentinel = "SYNTHETIC_PRIVATE_CONTENT_NOT_A_REAL_SECRET"
    async def handler(session, claimed):
        raise RuntimeError(sentinel)
    monkeypatch.setitem(run.HANDLERS, "audit_fixture", handler)
    saved = []
    async def capture(session, task_id, message, **kwargs):
        saved.append(message)
        return True
    monkeypatch.setattr(run, "_fail", capture)
    task = SimpleNamespace(id=uuid.uuid4(), kind="audit_fixture")
    await run._process(SimpleNamespace(rollback=AsyncMock()), SimpleNamespace(task=task))
    assert sentinel not in saved[0], "Raw exception content reached the queue failure persistence boundary"
