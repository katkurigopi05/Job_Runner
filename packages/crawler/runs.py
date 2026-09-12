"""Durable crawl state: what a cycle did, and how each board is holding up.

`CrawlReport` and `CompanyResult` describe a cycle to whoever is watching it
run. This writes the same facts down, because at 3,500 companies nobody is
watching: a cycle outlives the process, and once companies are dispatched
individually no single process sees the whole of one.

The scheduling half is the part with teeth. CLAUDE.md §9 records that 21 of
the original 50 seeds had left Greenhouse and answered 404 from both the board
API and the rendered page. A fixed poll interval spends the same budget on
those as on a board that posts weekly, forever. `record_attempt` backs a
failing board off geometrically instead, and a single success clears it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Company, CompanyCrawlState, CrawlRun
from packages.crawler.crawl import CompanyResult, CrawlReport, ResultHook

log = structlog.get_logger(__name__)

#: How many consecutive failures still double the wait. Also the guard that
#: keeps the arithmetic finite: `2 ** consecutive_failures` on a board that
#: has failed a few hundred times overflows `timedelta` outright, turning a
#: dead board into a crashed cycle.
MAX_BACKOFF_DOUBLINGS = 5

#: The most that backing off may *add* to a company's own interval.
#:
#: Additive rather than an absolute ceiling, which is the version this started
#: as and which quietly did nothing: a ceiling of 12h applied to a company
#: already polled every 24h clamps every backed-off delay back to 24h, so the
#: boards configured to be checked least often would have been the only ones
#: that never backed off at all.
MAX_BACKOFF = timedelta(hours=12)

#: Outcomes that mean the board did not give us postings. `blocked` counts:
#: robots.txt saying no is a perfectly normal answer, and the right response
#: to a normal "no" is to ask much less often.
FAILURE_STATUSES = frozenset({"error", "blocked", "suspect"})

#: Outcomes that are not attempts at all — nothing was fetched, so nothing
#: about the board's health was learned and its schedule must not move.
NON_ATTEMPT_STATUSES = frozenset({"skipped"})


def backoff_for(poll_interval_s: int, consecutive_failures: int) -> timedelta:
    """How long to wait before trying a board that has failed this often.

    Zero failures gives the company's configured interval — the healthy case
    is unchanged. Each failure doubles it, up to the two caps above.

    This is a *crawl schedule*, not a rate limit. It only ever makes the
    crawler wait longer, so it cannot interact with the §2.6 floors: those are
    enforced in `ratelimit.HostRateLimiter` on the way to the socket and apply
    whatever this returns.
    """
    interval = timedelta(seconds=poll_interval_s)
    if consecutive_failures <= 0:
        return interval
    grown = interval * (2 ** min(consecutive_failures, MAX_BACKOFF_DOUBLINGS))
    return min(grown, interval + MAX_BACKOFF)


async def start_run(session: AsyncSession, *, trigger: str, companies_total: int = 0) -> CrawlRun:
    """Open a run row. Does not commit."""
    run = CrawlRun(trigger=trigger, status="running", companies_total=companies_total)
    session.add(run)
    await session.flush()
    log.info("crawl_run_started", run_id=str(run.id), trigger=trigger, companies=companies_total)
    return run


async def finish_run(
    session: AsyncSession, run: CrawlRun, report: CrawlReport, *, status: str = "completed"
) -> CrawlRun:
    """Close a run row with the totals from `report`. Does not commit."""
    run.status = status
    run.finished_at = datetime.now(UTC)
    run.companies_fetched = report.fetched
    run.companies_skipped = len(report.blocked)
    run.companies_failed = len(report.failed)
    run.postings_new = sum(result.new_postings for result in report.results)
    run.postings_updated = sum(result.updated_postings for result in report.results)
    run.postings_closed = sum(result.closed_postings for result in report.results)
    run.suspect_companies = list(report.suspect)
    # `companies_total` is set when the run opens, but a run assembled from
    # dispatched tasks does not know its size up front. Never shrink it: a
    # smaller count here would hide a cycle that stopped early, which is the
    # one thing this column exists to make visible.
    run.companies_total = max(run.companies_total, len(report.results))
    await session.flush()
    log.info("crawl_run_finished", run_id=str(run.id), status=status, summary=report.summary())
    return run


async def get_state(session: AsyncSession, company_id: uuid.UUID) -> CompanyCrawlState | None:
    return await session.scalar(
        select(CompanyCrawlState).where(CompanyCrawlState.company_id == company_id)
    )


async def ensure_state(session: AsyncSession, company_id: uuid.UUID) -> CompanyCrawlState:
    """The state row for a company, created if this is its first sight.

    `ON CONFLICT DO NOTHING` rather than read-then-insert: two workers can
    reach a newly promoted company at the same time, and the unique constraint
    would otherwise turn that race into a failed cycle.
    """
    await session.execute(
        pg_insert(CompanyCrawlState)
        .values(company_id=company_id)
        .on_conflict_do_nothing(index_elements=["company_id"])
    )
    state = await get_state(session, company_id)
    assert state is not None  # just inserted, or already there
    return state


@dataclass
class AttemptOutcome:
    """What `record_attempt` decided, so a caller can log or assert on it."""

    status: str
    consecutive_failures: int
    next_due_at: datetime | None
    #: False when the result was not an attempt and state was left alone.
    recorded: bool = True


async def record_attempt(
    session: AsyncSession,
    company: Company,
    result: CompanyResult,
    *,
    run: CrawlRun | None = None,
    now: datetime | None = None,
) -> AttemptOutcome:
    """Fold one `CompanyResult` into that company's crawl state.

    Does not commit. Returns what it decided rather than nothing, because the
    interesting part — how long this board is now being left alone — is
    otherwise only visible by reading the row back.
    """
    status = result.status
    state = await ensure_state(session, company.id)

    if status in NON_ATTEMPT_STATUSES:
        # "Not due yet" and "no extractor for X" both land here. Neither
        # touched the network, so neither is evidence about the board, and
        # moving `next_due_at` on them would let a company that is merely not
        # due postpone itself indefinitely.
        return AttemptOutcome(status, state.consecutive_failures, state.next_due_at, recorded=False)

    current = now or datetime.now(UTC)
    state.last_attempt_at = current
    state.last_status = status
    state.last_error = result.error or (result.skipped_reason if result.blocked else None)
    if run is not None:
        state.last_run_id = run.id

    if status in FAILURE_STATUSES:
        state.consecutive_failures += 1
    else:
        state.consecutive_failures = 0
        state.last_success_at = current

    state.next_due_at = current + backoff_for(company.poll_interval_s, state.consecutive_failures)
    await session.flush()

    if state.consecutive_failures:
        log.info(
            "crawl_board_backoff",
            company=company.name,
            status=status,
            failures=state.consecutive_failures,
            next_due_at=state.next_due_at.isoformat(),
        )
    return AttemptOutcome(status, state.consecutive_failures, state.next_due_at)


async def due_companies(
    session: AsyncSession, *, limit: int, now: datetime | None = None
) -> list[Company]:
    """Companies whose next poll is owed, soonest first.

    One indexed range scan over `next_due_at`, which is why that column is
    materialized rather than computed from `last_polled_at + poll_interval_s`:
    the interval varies per row, so the computed form cannot use an index and
    reads the whole registry every tick.

    A NULL `next_due_at` means never crawled and sorts first — `NULLS FIRST`
    is Postgres's default for ascending order, but it is stated here because
    the behaviour is load-bearing rather than incidental.

    **Outer, not inner.** An inner join reads perfectly well and is wrong in
    the worst available way: a company with no state row does not come back
    *at all*, so it is never crawled and never appears in any report of what
    went wrong. The migration backfills every company that existed when it
    ran, which makes the bug invisible in exactly the situation a test would
    catch it — it only bites companies created afterwards, which is to say
    every company added by an import or promoted by discovery, which is to say
    all 3,500 of them. A missing row now reads as "due now", the same answer
    `is_due` gives for a company that has never been polled.

    The join costs the index its clean range scan for those rows. That is the
    right trade at this size — a registry of thousands is a scan measured in
    milliseconds — and the wrong one to revisit by narrowing the join back.
    """
    current = now or datetime.now(UTC)
    rows = await session.scalars(
        select(Company)
        .outerjoin(CompanyCrawlState, CompanyCrawlState.company_id == Company.id)
        .where(
            CompanyCrawlState.company_id.is_(None)
            | CompanyCrawlState.next_due_at.is_(None)
            | (CompanyCrawlState.next_due_at <= current)
        )
        .order_by(CompanyCrawlState.next_due_at.asc().nullsfirst())
        .limit(limit)
    )
    return list(rows.all())


def state_recorder(session: AsyncSession, run: CrawlRun | None = None) -> ResultHook:
    """A `crawl.ResultHook` that folds each outcome into crawl state.

    Handed to `crawl_all`, so the crawl module never has to know that crawl
    state exists — see `crawl.crawl_company` on why the dependency runs this
    way round.
    """

    async def record(company: Company | None, result: CompanyResult) -> None:
        if company is None:
            # The seed named an ATS with no extractor, so nothing was ever
            # upserted. There is no row to record health against, and the
            # missing extractor is a registry problem rather than a board one.
            return
        await record_attempt(session, company, result, run=run)

    return record
