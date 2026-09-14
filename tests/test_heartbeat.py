"""Worker liveness — the question no screen could answer.

An idle worker and a dead one were indistinguishable: both leave the queue
untouched. The heartbeat is what separates them, so these tests pin the three
states and the two properties that make it safe to run inside the worker —
it is throttled, and it never raises.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from packages.core.heartbeat import (
    STALE_AFTER_S,
    Heartbeat,
    WorkerState,
    classify,
    record,
)
from packages.core.models_ops import WorkerHeartbeat

START = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)


def _row(**overrides: object) -> WorkerHeartbeat:
    values: dict[str, object] = {
        "worker_id": "w1",
        "hostname": "host",
        "pid": 1,
        "started_at": START,
        "last_seen_at": START,
        "tasks_completed": 0,
        "stopped_at": None,
        "current_task_kind": None,
        "last_error_kind": None,
    }
    values.update(overrides)
    return WorkerHeartbeat(**values)


async def test_beats_upsert_one_row_per_worker_and_count_handled_tasks(db_session) -> None:
    await record(db_session, worker_id="w1", started_at=START, now=START)
    await record(
        db_session,
        worker_id="w1",
        started_at=START,
        completed=True,
        now=START + timedelta(seconds=5),
    )
    await record(
        db_session,
        worker_id="w1",
        started_at=START,
        completed=True,
        now=START + timedelta(seconds=9),
    )

    rows = (await db_session.scalars(select(WorkerHeartbeat))).all()
    assert len(rows) == 1
    assert rows[0].tasks_completed == 2
    assert rows[0].last_seen_at == START + timedelta(seconds=9)


async def test_an_error_survives_the_clean_beats_after_it(db_session) -> None:
    """Otherwise the next idle loop erases the only evidence of the failure."""
    await record(db_session, worker_id="w1", started_at=START, error_kind="OperationalError")
    await record(db_session, worker_id="w1", started_at=START)

    row = await db_session.scalar(select(WorkerHeartbeat).execution_options(populate_existing=True))
    assert row.last_error_kind == "OperationalError"


def test_a_recent_beat_is_alive_and_shows_its_task() -> None:
    view = classify(
        _row(current_task_kind="apply"), now=START + timedelta(seconds=STALE_AFTER_S - 1)
    )

    assert view.state is WorkerState.ALIVE
    assert view.current_task_kind == "apply"


def test_silence_without_a_goodbye_is_stale_not_idle() -> None:
    view = classify(
        _row(current_task_kind="apply"), now=START + timedelta(seconds=STALE_AFTER_S + 1)
    )

    assert view.state is WorkerState.STALE
    assert view.current_task_kind is None, "a dead worker is not running anything"


def test_a_clean_stop_reads_as_stopped_however_long_ago() -> None:
    view = classify(_row(stopped_at=START), now=START + timedelta(days=3))

    assert view.state is WorkerState.STOPPED


async def test_pulses_are_throttled_but_task_changes_are_not(committing_sessionmaker) -> None:
    ticks = iter([0.0, 1.0, 2.0, 20.0])
    beat = Heartbeat(
        "w-throttle", committing_sessionmaker, interval_s=15.0, clock=lambda: next(ticks)
    )

    assert await beat.pulse() is True, "the first beat always writes"
    assert await beat.pulse() is False, "one second later is not due"
    assert await beat.pulse(force=True, task_kind="crawl") is True, "a task start is shown at once"
    assert await beat.pulse() is True, "and the interval still applies afterwards"


async def test_a_heartbeat_failure_never_reaches_the_worker() -> None:
    def broken():
        raise RuntimeError("database unreachable")

    beat = Heartbeat("w-broken", broken)  # type: ignore[arg-type]

    assert await beat.pulse(force=True) is False


async def test_the_worker_loop_reports_the_task_it_handled(
    committing_sessionmaker, monkeypatch
) -> None:
    from apps.worker import run
    from packages.core import db as core_db
    from packages.core.queue import enqueue

    handled: list[str] = []

    async def noop(session, claimed) -> None:
        handled.append(claimed.task.kind)

    monkeypatch.setattr(core_db, "get_sessionmaker", lambda: committing_sessionmaker)
    monkeypatch.setattr(run, "HANDLERS", {"noop": noop})
    async with committing_sessionmaker() as session:
        await enqueue(session, "noop", {})
        await session.commit()

    beat = Heartbeat("w-loop", committing_sessionmaker)
    assert await run.run_once(worker_id="w-loop", heartbeat=beat) is True

    assert handled == ["noop"]
    async with committing_sessionmaker() as session:
        row = await session.get(WorkerHeartbeat, "w-loop")
    assert row is not None
    assert row.tasks_completed == 1
