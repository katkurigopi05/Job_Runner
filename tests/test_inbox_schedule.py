"""The managed inbox is a durable loop, not a handler only tests can call."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import func, select

from apps.worker import inbox_job
from packages.core.enums import QueueTaskStatus
from packages.core.models import QueueTask
from packages.core.queue import ClaimedTask, enqueue
from packages.inbox.imap import StaticMailSource


async def _inbox_tasks(session) -> list[QueueTask]:  # noqa: ANN001
    return list(
        (
            await session.scalars(
                select(QueueTask)
                .where(QueueTask.kind == inbox_job.INBOX_TASK_KIND)
                .order_by(QueueTask.created_at)
            )
        ).all()
    )


async def test_unconfigured_mailbox_creates_no_database_schedule(
    worker_session, monkeypatch
) -> None:
    monkeypatch.setattr(inbox_job, "build_mail_source", lambda: None)

    assert await inbox_job.ensure_inbox_poll(worker_session) is None
    await worker_session.commit()

    assert await _inbox_tasks(worker_session) == []


async def test_schedule_is_singleton_and_repairs_an_exhausted_poll(
    worker_session, monkeypatch
) -> None:
    monkeypatch.setattr(inbox_job, "build_mail_source", lambda: object())

    first = await inbox_job.ensure_inbox_poll(worker_session)
    await worker_session.commit()
    assert first is not None

    assert await inbox_job.ensure_inbox_poll(worker_session) is None
    await worker_session.commit()
    assert len(await _inbox_tasks(worker_session)) == 1

    first.status = QueueTaskStatus.FAILED.value
    await worker_session.commit()
    replacement = await inbox_job.ensure_inbox_poll(worker_session)
    await worker_session.commit()

    assert replacement is not None
    tasks = await _inbox_tasks(worker_session)
    assert [task.status for task in tasks] == [
        QueueTaskStatus.FAILED.value,
        QueueTaskStatus.PENDING.value,
    ]


async def test_two_supervisors_cannot_seed_two_polls(committing_sessionmaker, monkeypatch) -> None:
    """The advisory lock covers count through commit across processes."""
    monkeypatch.setattr(inbox_job, "build_mail_source", lambda: object())

    async def seed() -> object | None:
        async with committing_sessionmaker() as session:
            task = await inbox_job.ensure_inbox_poll(session)
            task_id = task.id if task is not None else None
            await session.commit()
            return task_id

    created = await asyncio.gather(seed(), seed())

    assert sum(task_id is not None for task_id in created) == 1
    async with committing_sessionmaker() as session:
        assert len(await _inbox_tasks(session)) == 1


async def test_successful_poll_commits_its_delayed_successor(worker_session, monkeypatch) -> None:
    source = StaticMailSource([], source_key="scheduled-empty-mailbox")
    monkeypatch.setattr(inbox_job, "build_mail_source", lambda: source)
    original = await enqueue(worker_session, inbox_job.INBOX_TASK_KIND, {})
    await worker_session.commit()
    before = await worker_session.scalar(select(func.clock_timestamp()))

    await inbox_job.handle_inbox(
        worker_session,
        ClaimedTask(task=original, reclaimed=False, previous_owner=None),
    )

    tasks = await _inbox_tasks(worker_session)
    successor = next(task for task in tasks if task.id != original.id)
    assert before is not None
    assert successor.run_after >= before + timedelta(seconds=50)


async def test_running_duplicate_does_not_prevent_the_future_successor(
    worker_session, monkeypatch
) -> None:
    """Two current deliveries still leave exactly one future delivery."""
    monkeypatch.setattr(inbox_job, "build_mail_source", lambda: object())
    first = await enqueue(worker_session, inbox_job.INBOX_TASK_KIND, {})
    second = await enqueue(worker_session, inbox_job.INBOX_TASK_KIND, {})
    first.status = QueueTaskStatus.RUNNING.value
    second.status = QueueTaskStatus.RUNNING.value
    await worker_session.commit()

    successor = await inbox_job.ensure_inbox_poll(
        worker_session,
        exclude_task_id=first.id,
        delay_seconds=60,
        source_configured=True,
    )
    await worker_session.commit()

    assert successor is not None
    tasks = await _inbox_tasks(worker_session)
    assert sum(task.status == QueueTaskStatus.PENDING.value for task in tasks) == 1


async def test_worker_supervisor_seeds_the_schedule(committing_sessionmaker, monkeypatch) -> None:
    from apps.worker import run as worker_run
    from packages.core import db as core_db

    monkeypatch.setattr(inbox_job, "build_mail_source", lambda: object())
    monkeypatch.setattr(core_db, "get_sessionmaker", lambda: committing_sessionmaker)
    monkeypatch.setattr(worker_run, "INBOX_SCHEDULE_TICK_SECONDS", 0.01)
    stop = asyncio.Event()
    supervisor = asyncio.create_task(worker_run._maintain_inbox_schedule(stop))
    try:

        async def schedule_exists() -> None:
            while True:
                async with committing_sessionmaker() as session:
                    if await _inbox_tasks(session):
                        return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(schedule_exists(), timeout=3)
    finally:
        stop.set()
        await asyncio.wait_for(supervisor, timeout=3)
