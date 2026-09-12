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
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Company, CrawlRun, QueueTask
from packages.core.queue import enqueue
from packages.crawler.crawl import CompanyResult, CrawlReport
from packages.crawler.extract import CompanySeed, extractor_for
from packages.crawler.runs import due_companies, finish_run, start_run

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
    await _claim(session, [c.id for c in companies if c.name not in set(report.unusable)])
    await session.flush()

    if report.unusable:
        log.warning(
            "crawl_dispatch_unusable_companies",
            count=len(report.unusable),
            companies=report.unusable[:20],
        )
    log.info("crawl_dispatched", run_id=str(run.id), summary=report.summary())
    return report


async def _claim(session: AsyncSession, company_ids: list[uuid.UUID]) -> None:
    """Push the dispatched companies' next poll out, provisionally.

    Without this the scheduler enqueues the same companies on every tick until
    their tasks are actually processed: `next_due_at` only moves when
    `record_attempt` runs, and a tick is far shorter than a crawl. Two minutes
    of that is the registry enqueued several times over, and the run
    accounting stops meaning anything.

    Provisional, because the authoritative value is written by
    `record_attempt` when the crawl reports. If the task is never processed —
    a worker lost, a queue drained — the company simply waits one ordinary
    interval and is picked up by a later tick. Self-healing, and in the
    direction of crawling *less* rather than more.
    """
    if not company_ids:
        return
    await session.execute(
        text("""
        UPDATE company_crawl_states AS s
           SET next_due_at = clock_timestamp() + make_interval(secs => c.poll_interval_s)
          FROM companies AS c
         WHERE c.id = s.company_id
           AND s.company_id = ANY(:ids)
        """),
        {"ids": company_ids},
    )
    # A company dispatched before it had a state row — promoted by discovery,
    # added by an import — needs one, or it is claimed by nothing and comes
    # back on the very next tick.
    await session.execute(
        text("""
        INSERT INTO company_crawl_states (company_id, next_due_at)
        SELECT c.id, clock_timestamp() + make_interval(secs => c.poll_interval_s)
          FROM companies AS c
         WHERE c.id = ANY(:ids)
        ON CONFLICT (company_id) DO NOTHING
        """),
        {"ids": company_ids},
    )


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
    return await due_companies(session, limit=limit)


__all__ = [
    "CRAWL_COMPANY_TASK_KIND",
    "backlog",
    "tick",
    "DispatchReport",
    "close_if_complete",
    "dispatch",
    "due_for_dispatch",
    "fold_into_run",
    "seed_from",
]


async def backlog(session: AsyncSession) -> int:
    """Crawl tasks enqueued and not yet finished."""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(QueueTask)
            .where(
                QueueTask.kind == CRAWL_COMPANY_TASK_KIND,
                QueueTask.status.in_(("pending", "running")),
            )
        )
        or 0
    )


async def tick(
    session: AsyncSession,
    *,
    limit: int,
    max_backlog: int,
    force: bool = False,
    trigger: str = "scheduled",
) -> DispatchReport | None:
    """One sweep: open a run, enqueue what is due, claim it. Does not commit.

    `None` when nothing was dispatched, which has two quite different causes
    and they are logged apart. Nothing is due — the ordinary state of a
    registry between polls — or the backlog is already too long, which is the
    crawler being told it is behind.

    Refusing to dispatch while behind is the point of `max_backlog`. A queue
    that keeps accepting work it cannot start does not go faster; it converts
    a slow cycle into an unbounded one, and hides how far behind it is inside
    a number nobody reads until the disk fills.
    """
    outstanding = await backlog(session)
    if outstanding >= max_backlog:
        log.warning("crawl_tick_skipped_backlog", outstanding=outstanding, limit=max_backlog)
        return None

    companies = await due_for_dispatch(
        session, limit=min(limit, max_backlog - outstanding), force=force
    )
    if not companies:
        log.debug("crawl_tick_nothing_due")
        return None

    run = await start_run(session, trigger=trigger)
    report = await dispatch(session, companies, run, force=force)
    if report.enqueued == 0:
        # Every candidate was unusable, so nothing will ever report and
        # nothing would ever close this run.
        await finish_run(session, run, CrawlReport())
    return report
