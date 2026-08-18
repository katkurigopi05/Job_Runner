"""Discovery handler — poll the aggregators, then grow the registry.

Runs on its own task kind rather than inside the crawl. The two answer
different questions on different clocks: the crawl re-reads boards the owner
chose and wants to be current, while discovery trawls for companies nobody
listed and is useful daily at most. Folding discovery into the crawl would
tie the slow, broad job to the fast, narrow one's schedule.

Idempotent, like every handler here: postings are keyed by a source-namespaced
external id, and promotion skips companies the registry already carries.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Posting, Profile
from packages.core.queue import ClaimedTask
from packages.crawler.discover import ingest, promote
from packages.crawler.fetch import build_fetcher
from packages.matching.score import embed_postings, score_and_store

log = structlog.get_logger(__name__)

DISCOVER_TASK_KIND = "discover"


async def handle_discover(session: AsyncSession, claimed: ClaimedTask) -> None:
    """Ingest every aggregator, promote what resolved, score what is new."""
    payload = claimed.task.payload_json or {}

    report = await ingest(
        session,
        build_fetcher(),
        limit=int(payload.get("limit", 500)),
        resolve_ats=bool(payload.get("resolve_ats", True)),
    )
    log.info("discovery_report", summary=report.summary())

    if payload.get("promote", True):
        report.promoted = await promote(session, seed_path=payload.get("seed_path"))

    if not report.new_postings:
        return

    postings = list(
        (await session.scalars(select(Posting).where(Posting.closed_at.is_(None)))).all()
    )
    await embed_postings(session, postings)

    for profile in (await session.scalars(select(Profile))).all():
        await score_and_store(session, profile, postings)
