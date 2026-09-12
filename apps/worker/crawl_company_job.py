"""Crawl one company, as its own queue task.

The cycle handler (`crawl_job`) polls the whole registry in one task. This
polls one company, so a cycle can be spread across workers and a worker that
dies costs one company rather than a night's crawl.

It deliberately does *not* do the scoring that `handle_crawl` does at the end
of a cycle. Scoring wants the corpus, and running it per company would mean
3,500 rebuilds of the document-frequency statistics per cycle, each one
invalidating the vectors the last had just written. What this records instead
is which postings changed, so the matching pass can work from that rather than
re-reading every open posting in the database.

Nothing here can cause an application to be submitted. It writes postings and
crawl state; the apply pipeline is reached only through the review queue and
`AUTO_SUBMIT`, and that path is untouched by anything in this file.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Company
from packages.core.queue import ClaimedTask
from packages.crawler.crawl import crawl_company
from packages.crawler.dispatch import (
    CRAWL_COMPANY_TASK_KIND,
    close_if_complete,
    fold_into_run,
    seed_from,
)
from packages.crawler.fetch import build_fetcher
from packages.crawler.runs import record_attempt

log = structlog.get_logger(__name__)

__all__ = ["CRAWL_COMPANY_TASK_KIND", "handle_crawl_company"]


class MalformedCrawlTask(ValueError):
    """The payload does not name a company this worker can crawl."""


def _company_id(payload: dict) -> uuid.UUID:
    raw = payload.get("company_id")
    if not raw:
        raise MalformedCrawlTask("crawl_company task has no company_id")
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise MalformedCrawlTask(f"company_id is not a uuid: {raw!r}") from exc


async def handle_crawl_company(session: AsyncSession, claimed: ClaimedTask) -> None:
    """Poll one company and fold the outcome into its run.

    Idempotent, as every handler here must be. Running it twice re-fetches the
    board and finds nothing changed — `_store` compares content hashes — so
    the second run emits no postings. It does count twice against the run's
    totals, which is the honest reading: the board really was fetched twice.
    """
    payload = claimed.task.payload_json or {}
    company_id = _company_id(payload)

    company = await session.get(Company, company_id)
    if company is None:
        # Deleted between dispatch and claim. Not an error to retry — there is
        # nothing to crawl and never will be.
        log.warning("crawl_company_gone", company_id=str(company_id))
        return

    seed = seed_from(company)
    if seed is None:
        raise MalformedCrawlTask(
            f"{company.name} has no crawlable board (slug={company.slug!r}, "
            f"ats={company.ats_type!r})"
        )

    run_id = uuid.UUID(payload["run_id"]) if payload.get("run_id") else None

    # `shared=True` rather than the setting: this handler exists so that
    # several workers crawl at once, and in-process counters would give each
    # of them its own §2.6 floor. See packages/crawler/dispatch.py.
    async with build_fetcher(shared=True) as fetcher:
        result = await crawl_company(session, seed, fetcher, force=bool(payload.get("force")))

    await record_attempt(session, company, result)
    if run_id is not None:
        await fold_into_run(session, run_id, result)
        await close_if_complete(session, run_id)

    log.info(
        "crawl_company_done",
        company=company.name,
        status=result.status,
        new=result.new_postings,
        updated=result.updated_postings,
        reopened=result.reopened_postings,
        closed=result.closed_postings,
    )
