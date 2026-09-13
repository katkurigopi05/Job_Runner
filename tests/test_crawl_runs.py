"""Durable crawl state: the run record, and per-board backoff.

The behaviour worth pinning here is not that rows get written — it is *which*
outcomes move a board's schedule. A cycle at 3,500 companies spends its whole
budget on the rate limiter, so a board that cannot be read has to stop costing
a slot, and a board that is merely not due yet must not be able to postpone
itself by being asked.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from packages.core.enums import SourceStatus
from packages.core.models import Company, CompanyCrawlState, CrawlRun
from packages.crawler.crawl import CompanyResult, CrawlReport, crawl_all, crawl_company
from packages.crawler.extract import CompanySeed
from packages.crawler.fetch import PoliteFetcher
from packages.crawler.ratelimit import HostRateLimiter
from packages.crawler.runs import (
    MAX_BACKOFF,
    backoff_for,
    due_companies,
    finish_run,
    record_attempt,
    start_run,
    state_recorder,
)

HOUR = 3600


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def limiter() -> HostRateLimiter:
    clock = FakeClock()
    return HostRateLimiter(clock=clock, sleeper=clock.sleep)


def _job(job_id: int):
    return {
        "id": job_id,
        "title": "Senior Backend Engineer",
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "location": {"name": "Remote"},
        "content": "<p>Python</p>",
    }


def _board_transport(payload: dict, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        return httpx.Response(status, text=json.dumps(payload))

    return httpx.MockTransport(handler)


@pytest.fixture
def seed() -> CompanySeed:
    return CompanySeed(name="Acme", slug="acme", poll_interval_s=HOUR)


# --------------------------------------------------------------------------
# Backoff arithmetic
# --------------------------------------------------------------------------


def test_a_healthy_board_keeps_its_own_interval() -> None:
    assert backoff_for(HOUR, 0) == timedelta(seconds=HOUR)


def test_each_failure_doubles_the_wait() -> None:
    assert backoff_for(HOUR, 1) == timedelta(hours=2)
    assert backoff_for(HOUR, 2) == timedelta(hours=4)
    assert backoff_for(HOUR, 3) == timedelta(hours=8)


def test_backoff_is_bounded_and_never_overflows() -> None:
    """A board that has failed hundreds of times is a dead board, not a crash.

    `2 ** consecutive_failures` is unbounded arithmetic feeding a `timedelta`,
    which raises `OverflowError` well before a long-dead board runs out of
    failures to accumulate.
    """
    assert backoff_for(HOUR, 500) == timedelta(seconds=HOUR) + MAX_BACKOFF
    assert backoff_for(HOUR, 10_000) == timedelta(seconds=HOUR) + MAX_BACKOFF


def test_a_rarely_polled_board_still_backs_off() -> None:
    """The cap is additive, so it cannot silently exempt slow-polled boards.

    An absolute ceiling of MAX_BACKOFF would clamp every backed-off delay for
    a daily board back down to its own interval — no backoff at all, for
    exactly the boards cheapest to leave alone.
    """
    daily = 24 * HOUR
    assert backoff_for(daily, 0) == timedelta(days=1)
    assert backoff_for(daily, 3) > timedelta(days=1)
    assert backoff_for(daily, 3) == timedelta(days=1) + MAX_BACKOFF


def test_backoff_never_shortens_an_interval() -> None:
    """It is a schedule, not a rate limit — it may only ever add delay."""
    for failures in range(0, 12):
        assert backoff_for(HOUR, failures) >= timedelta(seconds=HOUR)


# --------------------------------------------------------------------------
# What moves a board's schedule
# --------------------------------------------------------------------------


async def _company(db_session, name: str = "Acme") -> Company:
    company = Company(
        name=name,
        slug="acme",
        ats_type="greenhouse",
        poll_interval_s=HOUR,
        # See `runs.fetchable()`: only a verified board is fetch work.
        source_status=SourceStatus.VERIFIED.value,
    )
    db_session.add(company)
    await db_session.flush()
    return company


async def test_a_successful_crawl_clears_failures(db_session) -> None:
    company = await _company(db_session)
    failing = CompanyResult(company="Acme", fetched=True, error="HTTP 500")

    await record_attempt(db_session, company, failing)
    await record_attempt(db_session, company, failing)
    outcome = await record_attempt(
        db_session, company, CompanyResult(company="Acme", fetched=True, new_postings=1)
    )

    assert outcome.status == "ok"
    assert outcome.consecutive_failures == 0, "one good fetch clears the streak"


async def test_not_due_yet_does_not_move_the_schedule(db_session) -> None:
    """Otherwise a company could postpone itself indefinitely by being asked.

    `skipped` covers "not due yet" and "no extractor for X". Neither touched
    the network, so neither is evidence about the board.
    """
    company = await _company(db_session)
    now = datetime.now(UTC)
    await record_attempt(
        db_session, company, CompanyResult(company="Acme", new_postings=1), now=now
    )
    before = (await due_companies(db_session, limit=10, now=now + timedelta(days=400)))[0]
    state_before = await db_session.scalar(
        select(CompanyCrawlState).where(CompanyCrawlState.company_id == company.id)
    )

    outcome = await record_attempt(
        db_session,
        company,
        CompanyResult(company="Acme", skipped_reason="not due yet"),
        now=now + timedelta(hours=5),
    )

    assert outcome.recorded is False
    assert outcome.next_due_at == state_before.next_due_at
    assert before.id == company.id


@pytest.mark.parametrize(
    "result_kwargs",
    [
        {"error": "HTTP 500"},
        {"skipped_reason": "robots.txt: Disallow: /", "blocked": True},
        {"fetched": True, "suspect_parse": True},
    ],
)
async def test_failing_outcomes_back_the_board_off(db_session, result_kwargs) -> None:
    """A board we cannot read has to stop costing a slot every hour.

    `blocked` is included deliberately. robots.txt saying no is a normal
    answer per §2.6, and the right response to a normal "no" is to ask much
    less often — not to keep asking on the same schedule forever.
    """
    company = await _company(db_session)
    now = datetime.now(UTC)

    outcome = await record_attempt(
        db_session, company, CompanyResult(company="Acme", **result_kwargs), now=now
    )

    assert outcome.consecutive_failures == 1
    assert outcome.next_due_at > now + timedelta(seconds=HOUR)


async def test_a_backed_off_board_is_not_returned_as_due(db_session) -> None:
    company = await _company(db_session)
    now = datetime.now(UTC)
    await record_attempt(
        db_session, company, CompanyResult(company="Acme", error="HTTP 404"), now=now
    )

    assert await due_companies(db_session, limit=10, now=now + timedelta(minutes=30)) == []
    still_owed = await due_companies(db_session, limit=10, now=now + timedelta(days=2))
    assert [c.id for c in still_owed] == [company.id], "backing off is not giving up"


async def test_a_company_with_no_state_row_is_still_due(db_session) -> None:
    """The join must be outer, and this is the test that says why.

    `Fresh` has never been crawled, so nothing has created its
    `CompanyCrawlState` row. Under an inner join it does not come back at all
    — never crawled, never scheduled, and absent from every report of what
    went wrong, because a row that is not selected cannot be reported on.

    The migration backfills every company that existed when it ran, so the
    bug is invisible against seeded data and bites only companies created
    afterwards: everything an import adds, everything discovery promotes.
    """
    crawled = await _company(db_session, name="Crawled")
    fresh = await _company(db_session, name="Fresh")
    now = datetime.now(UTC)
    await record_attempt(
        db_session, crawled, CompanyResult(company="Crawled", new_postings=1), now=now
    )

    due = await due_companies(db_session, limit=10, now=now + timedelta(days=1))
    assert fresh.id in {company.id for company in due}, "a company with no state row is invisible"
    assert due[0].id == fresh.id, "and never crawled outranks merely overdue"


# --------------------------------------------------------------------------
# The run record
# --------------------------------------------------------------------------


async def test_a_cycle_writes_a_run_and_per_company_state(db_session, seed) -> None:
    fetcher = PoliteFetcher(
        transport=_board_transport({"jobs": [_job(1), _job(2)]}), rate_limiter=limiter()
    )
    run = await start_run(db_session, trigger="manual", companies_total=1)
    report = CrawlReport()

    await crawl_all(
        db_session,
        [seed],
        fetcher,
        force=True,
        on_result=state_recorder(db_session, run),
        report=report,
    )
    await finish_run(db_session, run, report)

    stored = await db_session.get(CrawlRun, run.id)
    assert stored.status == "completed"
    assert stored.finished_at is not None
    assert stored.postings_new == 2
    assert stored.companies_fetched == 1
    assert stored.suspect_companies == []

    company = await db_session.scalar(select(Company).where(Company.name == "Acme"))
    state = await db_session.scalar(
        select(CompanyCrawlState).where(CompanyCrawlState.company_id == company.id)
    )
    assert state.last_status == "ok"
    assert state.last_run_id == run.id
    assert state.consecutive_failures == 0


async def test_a_partial_cycle_still_reports_what_it_polled(db_session, seed) -> None:
    """The report is the caller's, so a cycle that dies keeps its progress.

    Built inside `crawl_all` it would go with the stack frame, and the run row
    would record zeros for a cycle that had in fact polled most of the
    registry — which reads as "nothing to do", the failure this repo keeps
    being bitten by.
    """
    fetcher = PoliteFetcher(transport=_board_transport({"jobs": [_job(1)]}), rate_limiter=limiter())
    run = await start_run(db_session, trigger="manual", companies_total=2)
    report = CrawlReport()

    await crawl_all(db_session, [seed], fetcher, force=True, report=report)
    # The second company never ran; close the run as the worker's abort path
    # would.
    await finish_run(db_session, run, report, status="aborted")

    stored = await db_session.get(CrawlRun, run.id)
    assert stored.status == "aborted"
    assert stored.postings_new == 1, "the company that did run is still counted"
    assert stored.companies_total == 2, "and the run still says how many it meant to poll"


async def test_a_suspect_board_is_named_in_the_run(db_session, seed) -> None:
    """`_close_missing`'s refusal case has to survive the process that saw it."""
    good = PoliteFetcher(transport=_board_transport({"jobs": [_job(1)]}), rate_limiter=limiter())
    await crawl_company(db_session, seed, good, force=True)

    empty = PoliteFetcher(transport=_board_transport({"jobs": []}), rate_limiter=limiter())
    run = await start_run(db_session, trigger="manual", companies_total=1)
    report = CrawlReport()
    await crawl_all(
        db_session,
        [seed],
        empty,
        force=True,
        on_result=state_recorder(db_session, run),
        report=report,
    )
    await finish_run(db_session, run, report)

    stored = await db_session.get(CrawlRun, run.id)
    assert stored.suspect_companies == ["Acme"]
    assert stored.postings_closed == 0, "a suspect parse closes nothing"
