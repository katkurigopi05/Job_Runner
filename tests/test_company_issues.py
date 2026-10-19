"""A company a worker could not read is noted, and the note closes when it can.

Asked for by the owner on 2026-10-19: note the company, move on to the next,
and look into the noted ones once everything else is done.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from packages.core.enums import SourceStatus
from packages.core.models import Company
from packages.core.models_issues import CompanyIssue
from packages.crawler import issues
from packages.crawler.crawl import CompanyResult
from packages.crawler.runs import record_attempt

NOW = datetime(2026, 10, 19, 22, 0, tzinfo=UTC)


async def _company(session, name: str = "Acme") -> Company:
    company = Company(name=name, supplied_website=f"https://{name.lower()}.com")
    session.add(company)
    await session.flush()
    return company


async def _issues(session, company) -> list[CompanyIssue]:
    rows = await session.scalars(
        select(CompanyIssue)
        .where(CompanyIssue.company_id == company.id)
        .order_by(CompanyIssue.noted_at)
        .execution_options(populate_existing=True)
    )
    return list(rows.all())


# --- noting --------------------------------------------------------------------------


async def test_a_failure_is_noted_once_and_counted_after(db_session) -> None:
    acme = await _company(db_session)

    for hours in (0, 6):
        await issues.note(
            db_session,
            acme.id,
            stage="discovery",
            category="unreachable",
            reason="its own site could not be fetched (HTTP 500)",
            now=NOW + timedelta(hours=hours),
        )

    [issue] = await _issues(db_session, acme)
    assert (issue.status, issue.times_noted) == ("noted", 2)
    assert issue.noted_at == NOW
    assert issue.last_noted_at == NOW + timedelta(hours=6)


async def test_discovery_and_crawl_are_noted_apart(db_session) -> None:
    acme = await _company(db_session)

    await issues.note(db_session, acme.id, stage="discovery", category="x", reason="r", now=NOW)
    await issues.note(
        db_session, acme.id, stage="crawl", category="board_gone", reason="r", now=NOW
    )

    assert sorted(i.stage for i in await _issues(db_session, acme)) == ["crawl", "discovery"]


async def test_the_same_failure_is_not_offered_to_the_deep_dive_again(db_session) -> None:
    """It would give the same answer, and the error list already has it."""
    acme = await _company(db_session)
    await issues.note(db_session, acme.id, stage="discovery", category="unreachable", reason="r")
    [issue] = await _issues(db_session, acme)
    issue.status = "needs_owner"
    await db_session.flush()

    await issues.note(db_session, acme.id, stage="discovery", category="unreachable", reason="r")

    [issue] = await _issues(db_session, acme)
    assert issue.status == "needs_owner"


async def test_a_different_failure_is_offered_again(db_session) -> None:
    acme = await _company(db_session)
    await issues.note(db_session, acme.id, stage="discovery", category="unreachable", reason="r")
    [issue] = await _issues(db_session, acme)
    issue.status = "needs_owner"
    await db_session.flush()

    await issues.note(
        db_session, acme.id, stage="discovery", category="careers_page_unreadable", reason="r2"
    )

    [issue] = await _issues(db_session, acme)
    assert (issue.status, issue.category, issue.reason) == (
        "noted",
        "careers_page_unreadable",
        "r2",
    )


# --- closing -------------------------------------------------------------------------


async def test_a_later_success_closes_the_note_and_keeps_it(db_session) -> None:
    acme = await _company(db_session)
    await issues.note(db_session, acme.id, stage="crawl", category="board_gone", reason="r")

    assert await issues.resolve(db_session, acme.id, stage="crawl", by="crawl", now=NOW)
    assert not await issues.resolve(db_session, acme.id, stage="crawl", by="crawl")

    [issue] = await _issues(db_session, acme)
    assert (issue.status, issue.resolved_by, issue.resolved_at) == ("fixed", "crawl", NOW)


async def test_a_failure_after_a_fix_opens_a_new_note(db_session) -> None:
    acme = await _company(db_session)
    await issues.note(
        db_session, acme.id, stage="crawl", category="board_gone", reason="r", now=NOW
    )
    await issues.resolve(db_session, acme.id, stage="crawl", by="crawl")

    later = NOW + timedelta(days=30)
    await issues.note(
        db_session, acme.id, stage="crawl", category="board_error", reason="r", now=later
    )

    assert [(i.status, i.category) for i in await _issues(db_session, acme)] == [
        ("fixed", "board_gone"),
        ("noted", "board_error"),
    ]


# --- handing notes to the deep dive --------------------------------------------------


async def test_notes_are_claimed_oldest_first_and_only_once(db_session) -> None:
    older, newer = await _company(db_session, "Older"), await _company(db_session, "Newer")
    await issues.note(db_session, newer.id, stage="discovery", category="x", reason="r", now=NOW)
    await issues.note(
        db_session, older.id, stage="discovery", category="x", reason="r", now=NOW - timedelta(1)
    )

    first = await issues.claim_for_deep_dive(db_session, limit=1, now=NOW)
    rest = await issues.claim_for_deep_dive(db_session, limit=10, now=NOW)
    again = await issues.claim_for_deep_dive(db_session, limit=10, now=NOW)

    [older_issue] = await _issues(db_session, older)
    assert first == [older_issue.id]
    assert len(rest) == 1
    assert again == [], "an enqueued note is not enqueued twice"
    assert older_issue.status == "queued"


async def test_a_deep_dive_lost_with_its_worker_is_offered_again_a_day_later(db_session) -> None:
    acme = await _company(db_session)
    await issues.note(db_session, acme.id, stage="discovery", category="x", reason="r", now=NOW)
    await issues.claim_for_deep_dive(db_session, limit=1, now=NOW)

    assert (
        await issues.claim_for_deep_dive(db_session, limit=1, now=NOW + timedelta(hours=23)) == []
    )
    assert len(await issues.claim_for_deep_dive(db_session, limit=1, now=NOW + timedelta(days=2)))


async def test_a_note_given_up_on_or_fixed_is_not_claimed(db_session) -> None:
    a, b = await _company(db_session, "A"), await _company(db_session, "B")
    for company in (a, b):
        await issues.note(db_session, company.id, stage="discovery", category="x", reason="r")
    [given_up] = await _issues(db_session, a)
    given_up.status = "needs_owner"
    await issues.resolve(db_session, b.id, stage="discovery", by="discovery")
    await db_session.flush()

    assert await issues.claim_for_deep_dive(db_session, limit=10) == []


# --- what a crawl failure is called --------------------------------------------------


def test_a_crawl_failure_has_a_word() -> None:
    assert issues.crawl_category(CompanyResult(company="a", error="HTTP 404")) == "board_gone"
    assert issues.crawl_category(CompanyResult(company="a", error="HTTP 410")) == "board_gone"
    assert issues.crawl_category(CompanyResult(company="a", error="ReadTimeout")) == "board_error"
    assert issues.crawl_category(CompanyResult(company="a", blocked=True)) == "board_blocked"
    assert issues.crawl_category(CompanyResult(company="a", suspect_parse=True)) == (
        "board_reads_empty"
    )
    assert issues.crawl_category(CompanyResult(company="a", fetched=True)) is None


# --- the crawl notes a board that keeps failing --------------------------------------


async def _board(session) -> Company:
    company = Company(
        name="Acme",
        slug="acme",
        ats_type="greenhouse",
        poll_interval_s=3600,
        source_status=SourceStatus.VERIFIED.value,
    )
    session.add(company)
    await session.flush()
    return company


async def test_a_board_failing_three_times_in_a_row_is_noted(db_session) -> None:
    acme = await _board(db_session)
    gone = CompanyResult(company="Acme", error="HTTP 404")

    for _ in range(issues.CRAWL_FAILURES_BEFORE_NOTED - 1):
        await record_attempt(db_session, acme, gone)
    assert await _issues(db_session, acme) == [], "one bad minute is not an issue"

    await record_attempt(db_session, acme, gone)

    [issue] = await _issues(db_session, acme)
    assert (issue.stage, issue.category, issue.reason) == ("crawl", "board_gone", "HTTP 404")


async def test_a_board_that_answers_again_closes_its_note(db_session) -> None:
    acme = await _board(db_session)
    for _ in range(issues.CRAWL_FAILURES_BEFORE_NOTED):
        await record_attempt(db_session, acme, CompanyResult(company="Acme", error="HTTP 404"))

    await record_attempt(db_session, acme, CompanyResult(company="Acme", fetched=True))

    [issue] = await _issues(db_session, acme)
    assert (issue.status, issue.resolved_by) == ("fixed", "crawl")


# --- the tick hands notes to the deep dive -------------------------------------------


async def _deep_dives(session) -> int:
    from packages.core.models import QueueTask

    return int(
        await session.scalar(
            select(func.count())
            .select_from(QueueTask)
            .where(QueueTask.kind == issues.DEEP_DIVE_TASK_KIND)
        )
    )


async def test_a_tick_enqueues_one_deep_dive_per_note_and_only_once(db_session) -> None:
    from packages.crawler.dispatch import tick

    for name in ("A", "B"):
        company = await _company(db_session, name)
        await issues.note(db_session, company.id, stage="discovery", category="x", reason="r")

    await tick(db_session, limit=10, max_backlog=100)
    await tick(db_session, limit=10, max_backlog=100)

    assert await _deep_dives(db_session) == 2


async def test_a_tick_stops_enqueuing_deep_dives_when_behind(db_session) -> None:
    from packages.crawler.dispatch import tick

    for name in ("A", "B", "C"):
        company = await _company(db_session, name)
        await issues.note(db_session, company.id, stage="discovery", category="x", reason="r")

    await tick(db_session, limit=10, max_backlog=2)

    assert await _deep_dives(db_session) == 2
