"""Deep dives start only when the owner says so.

The owner, 2026-10-19: the noted companies are looked into at the end, with
their permission. The first build dispatched a deep dive for every note on
every tick, and 2,343 were queued to start overnight without anyone asking.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, update

from packages.core.config import get_settings
from packages.core.models import Company, QueueTask
from packages.core.models_issues import CompanyIssue
from packages.crawler import issues
from packages.crawler.dispatch import tick


@pytest.fixture
def auto(monkeypatch):
    """Turn automatic deep dives on for one test, and back off after."""
    monkeypatch.setenv("DEEP_DIVE_AUTO", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _noted(session, *names: str) -> list[Company]:
    companies = []
    for name in names:
        company = Company(name=name)
        session.add(company)
        await session.flush()
        await issues.note(session, company.id, stage="discovery", category="x", reason="r")
        companies.append(company)
    return companies


async def _deep_dives(session, status: str = "pending") -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(QueueTask)
            .where(QueueTask.kind == issues.DEEP_DIVE_TASK_KIND, QueueTask.status == status)
        )
    )


async def _statuses(session) -> list[str]:
    rows = await session.scalars(
        select(CompanyIssue.status).execution_options(populate_existing=True)
    )
    return sorted(rows.all())


async def test_the_tick_does_not_start_deep_dives_by_itself(db_session) -> None:
    await _noted(db_session, "A", "B")

    await tick(db_session, limit=10, max_backlog=100)

    assert await _deep_dives(db_session) == 0
    assert await _statuses(db_session) == ["noted", "noted"]


async def test_with_the_owners_standing_permission_the_tick_starts_them(db_session, auto) -> None:
    await _noted(db_session, "A", "B")

    await tick(db_session, limit=10, max_backlog=100)

    assert await _deep_dives(db_session) == 2


async def test_the_owner_starts_every_noted_company_once(db_session) -> None:
    await _noted(db_session, "A", "B", "C")

    assert len(await issues.start_deep_dives(db_session)) == 3
    assert await issues.start_deep_dives(db_session) == [], "each has one waiting already"
    assert await _deep_dives(db_session) == 3
    assert await _statuses(db_session) == ["queued"] * 3


async def test_a_note_whose_deep_dive_was_cancelled_is_started_again(db_session) -> None:
    await _noted(db_session, "A")
    await issues.start_deep_dives(db_session)
    await db_session.execute(
        update(QueueTask)
        .where(QueueTask.kind == issues.DEEP_DIVE_TASK_KIND)
        .values(status="cancelled")
    )

    assert len(await issues.start_deep_dives(db_session)) == 1
    assert await _deep_dives(db_session) == 1


async def test_a_note_given_up_on_or_fixed_is_not_started(db_session) -> None:
    given_up, fixed = await _noted(db_session, "A", "B")
    await db_session.execute(
        update(CompanyIssue)
        .where(CompanyIssue.company_id == given_up.id)
        .values(status="needs_owner")
    )
    await issues.resolve(db_session, fixed.id, stage="discovery", by="discovery")

    assert await issues.start_deep_dives(db_session) == []


async def test_a_limit_starts_the_oldest_first(db_session) -> None:
    first, _second = await _noted(db_session, "First", "Second")

    [started] = await issues.start_deep_dives(db_session, limit=1)

    issue = await db_session.scalar(select(CompanyIssue).where(CompanyIssue.company_id == first.id))
    assert started == issue.id


async def test_the_owner_starts_them_from_the_api(client, committing_sessionmaker) -> None:
    async with committing_sessionmaker() as session:
        await _noted(session, "A", "B")
        await session.commit()

    before = (await client.get("/company-issues/summary")).json()
    started = (await client.post("/company-issues/deep-dive")).json()
    after = (await client.get("/company-issues/summary")).json()

    assert before["noted"] == 2 and before["waiting_for_permission"] == 2
    assert started == {"started": 2}
    assert after["queued"] == 2 and after["waiting_for_permission"] == 0
