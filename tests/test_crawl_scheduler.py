"""The sweep that keeps 3,500 companies moving without a timer thread.

A tick asks what is due, enqueues a bounded batch, and claims what it
enqueued. The three properties worth pinning are the ones that only go wrong
at scale: it must not re-enqueue companies whose tasks have not run yet, it
must stop when the workers are behind, and it must not multiply itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from apps.worker.crawl_job import CRAWL_TASK_KIND, handle_crawl
from packages.core.models import Company, CompanyCrawlState, CrawlRun, QueueTask
from packages.crawler.crawl import CompanyResult
from packages.crawler.dispatch import (
    CRAWL_COMPANY_TASK_KIND,
    backlog,
    tick,
)
from packages.crawler.runs import record_attempt


async def _registry(db_session, count: int) -> list[Company]:
    companies = [
        Company(name=f"Company {n}", slug=f"co{n}", ats_type="greenhouse", poll_interval_s=3600)
        for n in range(count)
    ]
    db_session.add_all(companies)
    await db_session.flush()
    return companies


async def _pending(db_session) -> int:
    return int(
        await db_session.scalar(
            select(func.count())
            .select_from(QueueTask)
            .where(QueueTask.kind == CRAWL_COMPANY_TASK_KIND)
        )
    )


async def test_a_tick_enqueues_what_is_due(db_session) -> None:
    await _registry(db_session, 3)

    report = await tick(db_session, limit=10, max_backlog=100)

    assert report is not None
    assert report.enqueued == 3
    assert await _pending(db_session) == 3


async def test_a_tick_is_capped(db_session) -> None:
    """A tick that enqueued the whole registry is one long cycle in disguise."""
    await _registry(db_session, 10)

    report = await tick(db_session, limit=4, max_backlog=100)

    assert report.enqueued == 4


async def test_the_next_tick_does_not_re_enqueue_the_same_companies(db_session) -> None:
    """The defect this claim exists to prevent.

    `next_due_at` only moves when the crawl reports, and a tick is far shorter
    than a crawl. Without claiming at dispatch, every tick re-enqueues
    everything still outstanding — two minutes of that is the registry
    enqueued several times over, and the run accounting stops meaning
    anything.
    """
    await _registry(db_session, 3)

    first = await tick(db_session, limit=10, max_backlog=100)
    second = await tick(db_session, limit=10, max_backlog=100)

    assert first.enqueued == 3
    assert second is None, "nothing is due; the first tick's companies are claimed"
    assert await _pending(db_session) == 3


async def test_a_claim_is_provisional_and_heals(db_session) -> None:
    """A task that never runs must not park its company for ever.

    The claim pushes the company out by one ordinary interval, not by a
    backoff and not indefinitely — if the worker was lost, a later tick picks
    it up again.
    """
    companies = await _registry(db_session, 1)
    await tick(db_session, limit=10, max_backlog=100)

    state = await db_session.scalar(
        select(CompanyCrawlState).where(CompanyCrawlState.company_id == companies[0].id)
    )
    assert state.next_due_at <= datetime.now(UTC) + timedelta(seconds=3601)
    assert state.next_due_at > datetime.now(UTC)


async def test_a_crawl_that_reports_overwrites_the_claim(db_session) -> None:
    """The claim is provisional; `record_attempt` is the authority."""
    companies = await _registry(db_session, 1)
    await tick(db_session, limit=10, max_backlog=100)

    outcome = await record_attempt(
        db_session, companies[0], CompanyResult(company="Company 0", error="HTTP 500")
    )

    assert outcome.consecutive_failures == 1
    assert outcome.next_due_at > datetime.now(UTC) + timedelta(seconds=3600), (
        "a failing board should be backed off past its ordinary interval"
    )


async def test_a_tick_stops_when_the_workers_are_behind(db_session) -> None:
    """An unbounded queue turns a slow cycle into an unbounded one.

    It does not go faster for accepting work it cannot start; it just hides
    how far behind it is inside a number nobody reads.
    """
    await _registry(db_session, 5)
    await tick(db_session, limit=2, max_backlog=100)
    assert await backlog(db_session) == 2

    blocked = await tick(db_session, limit=10, max_backlog=2)

    assert blocked is None
    assert await _pending(db_session) == 2, "nothing more was enqueued"


async def test_a_tick_never_exceeds_the_backlog_ceiling(db_session) -> None:
    """The batch is trimmed to the room left, not just refused at the line."""
    await _registry(db_session, 20)
    await tick(db_session, limit=5, max_backlog=100)

    await tick(db_session, limit=50, max_backlog=8)

    assert await backlog(db_session) <= 8


async def test_nothing_due_is_not_an_empty_run(db_session) -> None:
    """A run row per quiet tick would be thousands of rows saying nothing."""
    report = await tick(db_session, limit=10, max_backlog=100)

    assert report is None
    assert await db_session.scalar(select(func.count()).select_from(CrawlRun)) == 0


async def test_a_registry_of_unusable_rows_does_not_leave_a_run_open(db_session) -> None:
    """Nothing will ever report, so nothing would ever close it."""
    db_session.add(Company(name="No board", slug=None, ats_type="greenhouse"))
    await db_session.flush()

    report = await tick(db_session, limit=10, max_backlog=100)

    assert report.enqueued == 0
    run = await db_session.scalar(select(CrawlRun))
    assert run.status == "completed", "an unclosable run reads as a cycle still running"


@pytest.mark.parametrize("force", [False, True])
async def test_force_takes_the_registry_regardless_of_schedule(db_session, force) -> None:
    companies = await _registry(db_session, 2)
    now = datetime.now(UTC)
    for company in companies:
        await record_attempt(
            db_session, company, CompanyResult(company=company.name, new_postings=1), now=now
        )

    report = await tick(db_session, limit=10, max_backlog=100, force=force, trigger="manual")

    if force:
        assert report.enqueued == 2, "force ignores next_due_at"
    else:
        assert report is None, "nothing is due yet"


# --------------------------------------------------------------------------
# The sweep reschedules itself
# --------------------------------------------------------------------------


class _Claimed:
    def __init__(self, payload: dict) -> None:
        self.task = QueueTask(kind=CRAWL_TASK_KIND, payload_json=payload)


async def _sweeps(db_session) -> list[QueueTask]:
    rows = await db_session.scalars(
        select(QueueTask).where(QueueTask.kind == CRAWL_TASK_KIND, QueueTask.status == "pending")
    )
    return list(rows.all())


async def test_a_sweep_enqueues_the_next_one(db_session) -> None:
    """A task row is a schedule anyone can read, and the queue already
    survives restarts — so there is no timer thread to keep alive."""
    await _registry(db_session, 1)

    await handle_crawl(db_session, _Claimed({"dispatch": True}))

    sweeps = await _sweeps(db_session)
    assert len(sweeps) == 1
    assert sweeps[0].run_after > datetime.now(UTC)


async def test_a_sweep_does_not_multiply(db_session) -> None:
    """Every sweep enqueuing a successor unconditionally doubles the number
    of sweeps each time two ever run at once — and at-least-once delivery
    guarantees two eventually will."""
    await _registry(db_session, 1)

    for _ in range(4):
        await handle_crawl(db_session, _Claimed({"dispatch": True}))

    assert len(await _sweeps(db_session)) == 1, "one pending sweep is all this needs"


async def test_a_one_shot_sweep_does_not_reschedule(db_session) -> None:
    """`repeat: false` is how a manual sweep stays a single sweep."""
    await _registry(db_session, 1)

    await handle_crawl(db_session, _Claimed({"dispatch": True, "repeat": False}))

    assert await _sweeps(db_session) == []


async def test_a_quiet_sweep_still_schedules_the_next(db_session) -> None:
    """Nothing due must not mean the cycle stops.

    This is the failure that ends a crawler quietly: one tick finds an empty
    registry, declines to reschedule, and nothing ever runs again.
    """
    await handle_crawl(db_session, _Claimed({"dispatch": True}))

    assert len(await _sweeps(db_session)) == 1
