"""Crawl handler — poll the registry, then score what came back.

Runs as a queue task so it shares the worker's lease machinery: a crawl cycle
over 50 companies at a 60s floor takes at least 50 minutes, which is far
longer than the default lease. Lease renewal (apps/worker/run.py) is what
keeps another worker from reclaiming the task mid-cycle.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Posting, Profile
from packages.core.queue import ClaimedTask
from packages.crawler.crawl import crawl_all
from packages.crawler.extract import load_seed
from packages.crawler.fetch import build_fetcher
from packages.matching.score import embed_postings, score_and_store

log = structlog.get_logger(__name__)

CRAWL_TASK_KIND = "crawl"


async def handle_crawl(session: AsyncSession, claimed: ClaimedTask) -> None:
    """Run one crawl cycle and score the postings it produced.

    Idempotent: change detection means a repeat run emits nothing, and Match
    rows are upserted rather than appended.
    """
    payload = claimed.task.payload_json or {}
    seed_path = payload.get("seed_path")
    force = bool(payload.get("force"))

    seeds = load_seed(seed_path)
    if not seeds:
        log.warning("crawl_no_seeds", seed_path=seed_path)
        return

    fetcher = build_fetcher()
    report = await crawl_all(session, seeds, fetcher, force=force)
    log.info("crawl_done", summary=report.summary())

    if not report.emitted:
        return

    # Only postings that are actually open are worth embedding or scoring.
    postings = list(
        (await session.scalars(select(Posting).where(Posting.closed_at.is_(None)))).all()
    )
    embedded = await embed_postings(session, postings)
    log.info("postings_embedded", count=embedded)

    profiles = list((await session.scalars(select(Profile))).all())
    for profile in profiles:
        await score_and_store(session, profile, postings)
