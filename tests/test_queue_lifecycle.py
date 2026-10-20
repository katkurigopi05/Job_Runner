"""A task's life after it is claimed: retried with backoff, cancelled, timed out,
timed, and cleared away once finished.

Asked for by the owner on 2026-10-19, after comparing the queue with a
crawler-style job system: it retried at a fixed 60 seconds, a pending task
could not be cancelled, a handler that hung held its worker for ever, nothing
recorded when a task finished, and 63,436 finished tasks had never been
cleared.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text, update

from packages.core.enums import QueueTaskStatus
from packages.core.models import QueueTask
from packages.core.queue import (
    RetryPolicy,
    cancel_pending,
    claim_task,
    complete_task,
    enqueue,
    fail_task,
    prune_finished,
    queue_stats,
)

WORKER = "test-worker"


async def _claimed(session, kind: str = "crawl_company"):
    await enqueue(session, kind, {"n": 1})
    claimed = await claim_task(session, worker_id=WORKER, kinds=[kind])
    assert claimed is not None
    return claimed


async def _seconds_until_run(session, task_id) -> float:
    return float(
        await session.scalar(
            select(func.extract("epoch", QueueTask.run_after - func.clock_timestamp())).where(
                QueueTask.id == task_id
            )
        )
    )


async def _due_now(session, task_id) -> None:
    await session.execute(
        update(QueueTask).where(QueueTask.id == task_id).values(run_after=func.now())
    )


# --- 1. retries back off --------------------------------------------------------------


def test_each_retry_waits_twice_as_long_up_to_a_cap() -> None:
    policy = RetryPolicy(base_s=60, max_delay_s=600)

    assert [policy.delay_after(attempt) for attempt in (1, 2, 3, 4, 5)] == [
        60,
        120,
        240,
        480,
        600,
    ]


async def test_a_failure_waits_longer_each_time(db_session) -> None:
    policy = RetryPolicy(base_s=100, max_delay_s=10_000, max_attempts=5)
    claimed = await _claimed(db_session)
    task_id = claimed.task.id

    assert await fail_task(db_session, claimed, "HTTP 500", policy=policy) is True
    first = await _seconds_until_run(db_session, task_id)
    await _due_now(db_session, task_id)
    again = await claim_task(db_session, worker_id=WORKER, max_attempts=5)
    assert again is not None
    assert await fail_task(db_session, again, "HTTP 500", policy=policy) is True
    second = await _seconds_until_run(db_session, task_id)

    assert 90 < first < 110
    assert 190 < second < 210


async def test_the_last_attempt_fails_for_good(db_session) -> None:
    claimed = await _claimed(db_session)

    assert await fail_task(db_session, claimed, "x", policy=RetryPolicy(max_attempts=1)) is False
    assert claimed.task.status == QueueTaskStatus.FAILED.value


# --- 4. a task records when it finished ----------------------------------------------


async def test_a_finished_task_records_when(db_session) -> None:
    claimed = await _claimed(db_session)

    assert await complete_task(db_session, claimed)

    task = claimed.task
    assert task.finished_at is not None
    assert task.locked_at is not None and task.finished_at >= task.locked_at


async def test_a_task_failed_for_good_records_when_and_a_retry_does_not(db_session) -> None:
    retried = await _claimed(db_session)
    await fail_task(db_session, retried, "x", policy=RetryPolicy(max_attempts=3))
    failed = await _claimed(db_session, "inbox")
    await fail_task(db_session, failed, "x", policy=RetryPolicy(max_attempts=1))

    assert retried.task.finished_at is None
    assert failed.task.finished_at is not None


async def test_a_task_out_of_attempts_at_claim_records_when(db_session) -> None:
    claimed = await _claimed(db_session)
    await db_session.execute(
        update(QueueTask)
        .where(QueueTask.id == claimed.task.id)
        .values(attempts=3, lease_expires_at=func.now() - timedelta(minutes=1))
    )

    assert await claim_task(db_session, worker_id="another") is None

    await db_session.refresh(claimed.task)
    assert claimed.task.status == QueueTaskStatus.FAILED.value
    assert claimed.task.finished_at is not None


# --- 2. a pending task can be cancelled ----------------------------------------------


async def test_a_pending_task_is_cancelled_and_never_claimed(db_session) -> None:
    task = await enqueue(db_session, "discover_company", {"company_id": "a"})

    cancelled = await cancel_pending(db_session, task_id=task.id, reason="not needed")

    await db_session.refresh(task)
    assert cancelled == [task.id]
    assert (task.status, task.last_error) == ("cancelled", "cancelled: not needed")
    assert task.finished_at is not None
    assert await claim_task(db_session, worker_id=WORKER) is None


async def test_a_running_task_is_not_cancelled(db_session) -> None:
    claimed = await _claimed(db_session)

    assert await cancel_pending(db_session, task_id=claimed.task.id, reason="x") == []

    await db_session.refresh(claimed.task)
    assert claimed.task.status == QueueTaskStatus.RUNNING.value


async def test_every_pending_task_of_a_kind_can_be_cancelled_at_once(db_session) -> None:
    for n in range(3):
        await enqueue(db_session, "discover_company", {"company_id": str(n)})
    kept = await enqueue(db_session, "crawl_company", {"company_id": "x"})

    cancelled = await cancel_pending(db_session, kind="discover_company", reason="x")

    await db_session.refresh(kept)
    assert len(cancelled) == 3
    assert kept.status == QueueTaskStatus.PENDING.value


async def test_a_kind_that_must_not_be_cancelled_is_refused(db_session) -> None:
    task = await enqueue(db_session, "apply", {"application_id": "a"})

    assert (
        await cancel_pending(db_session, task_id=task.id, reason="x", refuse_kinds={"apply"}) == []
    )
    assert await cancel_pending(db_session, kind="apply", reason="x", refuse_kinds={"apply"}) == []


# --- 5. finished tasks are cleared away ----------------------------------------------


async def _finished(session, kind: str, status: str, days_ago: float) -> QueueTask:
    task = await enqueue(session, kind, {})
    await session.execute(
        update(QueueTask)
        .where(QueueTask.id == task.id)
        .values(
            status=status,
            finished_at=func.now() - timedelta(days=days_ago),
            created_at=func.now() - timedelta(days=days_ago),
        )
    )
    return task


async def _ids(session) -> set:
    return set((await session.scalars(select(QueueTask.id))).all())


async def test_old_finished_tasks_are_cleared_and_recent_ones_kept(db_session) -> None:
    old_done = await _finished(db_session, "crawl_company", "done", 10)
    await _finished(db_session, "crawl_company", "done", 1)  # newest of its kind, and recent
    recent_done = await _finished(db_session, "crawl_company", "done", 2)
    old_failed = await _finished(db_session, "crawl_company", "failed", 40)
    recent_failed = await _finished(db_session, "crawl_company", "failed", 10)
    old_cancelled = await _finished(db_session, "crawl_company", "cancelled", 10)
    pending = await enqueue(db_session, "crawl_company", {})

    removed = await prune_finished(
        db_session, keep_done=timedelta(days=7), keep_failed=timedelta(days=30)
    )

    left = await _ids(db_session)
    assert removed == 3
    assert old_done.id not in left and old_failed.id not in left
    assert old_cancelled.id not in left
    assert {recent_done.id, recent_failed.id, pending.id} <= left


async def test_the_newest_finished_task_of_each_kind_is_always_kept(db_session) -> None:
    """The crawl status shows when the last crawl finished, however long ago."""
    last_tick = await _finished(db_session, "crawl", "done", 60)
    older_tick = await _finished(db_session, "crawl", "done", 61)

    await prune_finished(db_session, keep_done=timedelta(days=7), keep_failed=timedelta(days=30))

    left = await _ids(db_session)
    assert last_tick.id in left
    assert older_tick.id not in left


async def test_a_task_finished_before_the_finish_time_existed_is_aged_by_creation(
    db_session,
) -> None:
    legacy = await _finished(db_session, "crawl_company", "done", 20)
    await _finished(db_session, "crawl_company", "done", 0)
    await db_session.execute(
        update(QueueTask).where(QueueTask.id == legacy.id).values(finished_at=None)
    )

    await prune_finished(db_session, keep_done=timedelta(days=7), keep_failed=timedelta(days=30))

    assert legacy.id not in await _ids(db_session)


# --- 4. what the queue is doing ------------------------------------------------------


async def test_the_stats_count_each_kind_and_time_what_finished(db_session) -> None:
    await enqueue(db_session, "discover_company", {})
    done = await _claimed(db_session, "crawl_company")
    await complete_task(db_session, done)
    await db_session.execute(
        update(QueueTask)
        .where(QueueTask.id == done.task.id)
        .values(finished_at=QueueTask.locked_at + timedelta(seconds=30))
    )
    failed = await _claimed(db_session, "crawl_company")
    await fail_task(db_session, failed, "HTTP 404", policy=RetryPolicy(max_attempts=1))

    stats = {row.kind: row for row in await queue_stats(db_session, since=timedelta(hours=24))}

    crawl = stats["crawl_company"]
    assert (crawl.done, crawl.failed, crawl.pending, crawl.running) == (1, 1, 0, 0)
    assert crawl.median_seconds == 30
    assert crawl.last_error == "HTTP 404"
    assert stats["discover_company"].pending == 1
    assert stats["discover_company"].oldest_waiting_seconds is not None


# --- 3. a handler that hangs gives its worker back -----------------------------------


async def test_a_handler_past_its_time_limit_is_stopped_and_retried(
    committing_sessionmaker, monkeypatch
) -> None:
    from apps.worker import run
    from packages.core import db as core_db

    async def hangs(session, claimed) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(core_db, "get_sessionmaker", lambda: committing_sessionmaker)
    monkeypatch.setattr(run, "HANDLERS", {"hangs": hangs})
    monkeypatch.setattr(run, "TIME_LIMITS", {"hangs": 0.2})
    async with committing_sessionmaker() as session:
        task = await enqueue(session, "hangs", {})
        await session.commit()

    started = datetime.now(UTC)
    assert await run.run_once(worker_id="w-timeout") is True

    assert datetime.now(UTC) - started < timedelta(seconds=10)
    async with committing_sessionmaker() as session:
        row = await session.get(QueueTask, task.id)
        assert row is not None
        assert (row.status, row.last_error) == ("pending", "timed out after 0.2s")
        assert row.attempts == 1


async def test_a_timeout_raised_by_the_handler_itself_is_an_ordinary_failure(
    committing_sessionmaker, monkeypatch
) -> None:
    from apps.worker import run
    from packages.core import db as core_db

    async def times_out(session, claimed) -> None:
        raise TimeoutError

    monkeypatch.setattr(core_db, "get_sessionmaker", lambda: committing_sessionmaker)
    monkeypatch.setattr(run, "HANDLERS", {"own_timeout": times_out})
    async with committing_sessionmaker() as session:
        task = await enqueue(session, "own_timeout", {})
        await session.commit()

    await run.run_once(worker_id="w-timeout")

    async with committing_sessionmaker() as session:
        row = await session.get(QueueTask, task.id)
        assert row is not None and row.last_error == "TimeoutError"


def test_every_kind_has_a_retry_policy_and_a_time_limit() -> None:
    from apps.worker import run

    for kind in run.HANDLERS:
        assert run.retry_policy(kind).max_attempts >= 1
        assert run.time_limit(kind) > 0


async def test_the_database_clock_is_what_ages_a_task(db_session) -> None:
    """Sanity: `now()` in these tests is the transaction's clock."""
    assert await db_session.scalar(text("select now() <= clock_timestamp()"))


async def test_the_worker_prunes_by_the_settings_and_one_pool_at_a_time(
    committing_sessionmaker, monkeypatch
) -> None:
    from apps.worker import run
    from packages.core import db as core_db

    monkeypatch.setattr(core_db, "get_sessionmaker", lambda: committing_sessionmaker)
    async with committing_sessionmaker() as session:
        old = await _finished(session, "crawl_company", "done", 10)
        await _finished(session, "crawl_company", "done", 0)
        await session.commit()

    # Another pool holds the lock: this one skips.
    async with committing_sessionmaker() as holder:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": run._PRUNE_LOCK_KEY}
        )
        assert await run.prune_queue_once() == 0

    assert await run.prune_queue_once() == 1
    async with committing_sessionmaker() as session:
        assert old.id not in await _ids(session)
