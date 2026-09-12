"""Turning one long cycle into many short tasks.

A cycle is a `for` loop over the registry inside a single queue task. At 29
companies that is a few minutes. At 3,500 it is a task that runs for many
hours, holds one lease the whole time, and loses everything after the failure
point if it dies — and it can only ever use one worker, because a loop is a
loop.

Dispatching turns it into one task per company. The work is the same and calls
the same `crawl_company`; what changes is that it is now divisible. Several
workers can take companies at once, a worker that dies loses one company
rather than a night, and a company that fails is retried by the queue's own
machinery instead of by rewriting the cycle.

**This requires the shared rate limiter.** Several workers crawling at once
with in-process counters means each has its own idea of when a host was last
touched, so N workers make the §2.6 floor the floor divided by N. The
dispatcher asks for `shared=True` explicitly rather than trusting a setting,
because the setting can be off and the breach is silent — the traffic simply
gets faster and the people who notice are at the far end.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Company, CrawlRun
from packages.core.queue import enqueue
from packages.crawler.crawl import CompanyResult
from packages.crawler.extract import CompanySeed, extractor_for

log = structlog.get_logger(__name__)

#: One company, one task.
CRAWL_COMPANY_TASK_KIND = "crawl_company"


@dataclass
class DispatchReport:
    run_id: uuid.UUID
    enqueued: int = 0
    #: Companies skipped because the row cannot name a board to fetch. Listed
    #: rather than counted: each one is a registry entry that will never be
    #: crawled until someone fixes it, which is a silence worth being able to
    #: read.
    unusable: list[str] | None = None

    def summary(self) -> str:
        unusable = len(self.unusable or ())
        return f"{self.enqueued} companies dispatched, {unusable} unusable"


def seed_from(company: Company) -> CompanySeed | None:
    """Rebuild the crawl input from the registry row alone.

    This is what `Company.slug` was added for. A dispatched task carries an
    id, so the handler that picks it up cannot read the seed file — it may
    have been edited or reordered since, and at 3,500 entries re-parsing it
    per company would cost more than the fetch.

    `None` when the row cannot name a board: no slug, or an ATS with no
    extractor. Both are registry problems, and enqueuing a task that is
    certain to fail would convert them into queue noise.
    """
    if not company.slug or not company.ats_type:
        return None
    if extractor_for(company.ats_type) is None:
        return None
    return CompanySeed(
        name=company.name,
        slug=company.slug,
        ats=company.ats_type,
        careers_url=company.careers_url,
        domain=company.domain,
        poll_interval_s=company.poll_interval_s,
    )


async def dispatch(
    session: AsyncSession,
    companies: list[Company],
    run: CrawlRun,
    *,
    force: bool = False,
) -> DispatchReport:
    """Enqueue one crawl task per company. Does not commit."""
    report = DispatchReport(run_id=run.id, unusable=[])

    for company in companies:
        seed = seed_from(company)
        if seed is None:
            report.unusable.append(company.name)
            continue
        await enqueue(
            session,
            CRAWL_COMPANY_TASK_KIND,
            {"company_id": str(company.id), "run_id": str(run.id), "force": force},
        )
        report.enqueued += 1

    # The run's size is what it actually dispatched, not what it was handed.
    # A run whose total counted companies it declined to enqueue would never
    # reach its total and would sit `running` for ever.
    run.companies_total = report.enqueued
    await session.flush()

    if report.unusable:
        log.warning(
            "crawl_dispatch_unusable_companies",
            count=len(report.unusable),
            companies=report.unusable[:20],
        )
    log.info("crawl_dispatched", run_id=str(run.id), summary=report.summary())
    return report


#: Folded into the run with SQL arithmetic rather than read-modify-write.
#: Every company task updates the same row, so `run.postings_new += n` in
#: Python is two workers reading the same number and both writing it back.
_COUNTER_FOR_STATUS = {
    "ok": "companies_fetched",
    "unchanged": "companies_fetched",
    "suspect": "companies_fetched",
    "blocked": "companies_skipped",
    "skipped": "companies_skipped",
    "error": "companies_failed",
}


async def fold_into_run(session: AsyncSession, run_id: uuid.UUID, result: CompanyResult) -> None:
    """Add one company's outcome to the run row. Does not commit."""
    counter = _COUNTER_FOR_STATUS.get(result.status, "companies_failed")
    values = {
        counter: getattr(CrawlRun, counter) + 1,
        "postings_new": CrawlRun.postings_new + result.new_postings,
        "postings_updated": CrawlRun.postings_updated + result.updated_postings,
        "postings_closed": CrawlRun.postings_closed + result.closed_postings,
    }
    if result.suspect_parse:
        # `||` on the JSONB column, for the same reason as the counters: two
        # workers appending in Python would each write a list missing the
        # other's name.
        values["suspect_companies"] = CrawlRun.suspect_companies.concat(
            func.to_jsonb(func.cast([result.company], CrawlRun.suspect_companies.type))
        )
    await session.execute(update(CrawlRun).where(CrawlRun.id == run_id).values(**values))


async def close_if_complete(session: AsyncSession, run_id: uuid.UUID) -> bool:
    """Mark the run finished once every dispatched company has reported.

    No single process sees the whole cycle any more, so the run cannot be
    closed by whoever started it. The last company to report closes it, and
    "last" is decided by the database rather than by counting in memory.

    The `status = 'running'` in the WHERE clause is what makes that safe under
    at-least-once delivery: two tasks finishing together both see a complete
    count, both try to close, and exactly one UPDATE matches a row.
    """
    result = await session.execute(
        update(CrawlRun)
        .where(
            CrawlRun.id == run_id,
            CrawlRun.status == "running",
            CrawlRun.companies_fetched + CrawlRun.companies_skipped + CrawlRun.companies_failed
            >= CrawlRun.companies_total,
        )
        .values(status="completed", finished_at=func.clock_timestamp())
    )
    closed = bool(result.rowcount)
    if closed:
        log.info("crawl_run_finished", run_id=str(run_id), status="completed")
    return closed


async def due_for_dispatch(
    session: AsyncSession, *, limit: int, force: bool = False
) -> list[Company]:
    """Companies to enqueue this tick.

    `force` takes everything in registry order; otherwise only what is owed,
    soonest first. Both are capped — a tick that enqueued 3,500 tasks at once
    would be one long cycle again, wearing a queue as a disguise.
    """
    if force:
        rows = await session.scalars(select(Company).order_by(Company.name).limit(limit))
        return list(rows.all())

    from packages.crawler.runs import due_companies

    return await due_companies(session, limit=limit)


__all__ = [
    "CRAWL_COMPANY_TASK_KIND",
    "DispatchReport",
    "close_if_complete",
    "dispatch",
    "due_for_dispatch",
    "fold_into_run",
    "seed_from",
]
