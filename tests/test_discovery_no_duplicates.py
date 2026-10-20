"""A company with a discovery task waiting or running is not queued again.

Found on 2026-10-19 with 3,974 discovery tasks queued for 2,621 companies:
dispatch marks a company taken for one hour (`dispatch_discovery`), the queue
was five hours long, so every tick queued each still-waiting company again.
1,353 were queued twice, each duplicate a minute of a worker spent re-reading
a site, and each run bumped the company's attempts, pushing its next retry out.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from packages.core.enums import SourceStatus
from packages.core.models import Company, CompanyCrawlState, QueueTask
from packages.core.queue import enqueue
from packages.crawler.dispatch import DISCOVER_COMPANY_TASK_KIND, tick
from packages.crawler.runs import due_for_discovery

LAPSED = datetime.now(UTC) - timedelta(hours=2)


async def _failed(session, name: str) -> Company:
    company = Company(name=name, source_status=SourceStatus.FAILED.value)
    session.add(company)
    await session.flush()
    session.add(CompanyCrawlState(company_id=company.id, discovery_next_at=LAPSED))
    await session.flush()
    return company


async def _queued(session, company: Company, status: str = "pending") -> None:
    task = await enqueue(session, DISCOVER_COMPANY_TASK_KIND, {"company_id": str(company.id)})
    task.status = status
    await session.flush()


async def test_a_company_already_queued_is_not_due_again(db_session) -> None:
    waiting, running, free = (
        await _failed(db_session, "Waiting"),
        await _failed(db_session, "Running"),
        await _failed(db_session, "Free"),
    )
    await _queued(db_session, waiting)
    await _queued(db_session, running, status="running")

    due = await due_for_discovery(db_session, limit=10)

    assert [company.name for company in due] == [free.name]


async def test_a_finished_or_cancelled_task_does_not_hold_a_company_back(db_session) -> None:
    done, cancelled = await _failed(db_session, "Done"), await _failed(db_session, "Cancelled")
    await _queued(db_session, done, status="done")
    await _queued(db_session, cancelled, status="cancelled")

    assert {c.name for c in await due_for_discovery(db_session, limit=10)} == {
        "Done",
        "Cancelled",
    }


async def test_ticks_while_the_queue_is_long_queue_each_company_once(db_session) -> None:
    for name in ("A", "B"):
        await _failed(db_session, name)

    await tick(db_session, limit=10, max_backlog=100)
    # The one-hour claim lapses while the tasks still wait in a long queue.
    await db_session.execute(CompanyCrawlState.__table__.update().values(discovery_next_at=LAPSED))
    await tick(db_session, limit=10, max_backlog=100)

    queued = await db_session.scalar(
        select(func.count())
        .select_from(QueueTask)
        .where(QueueTask.kind == DISCOVER_COMPANY_TASK_KIND)
    )
    assert queued == 2
