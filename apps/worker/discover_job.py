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
from packages.crawler.discover import ingest, promote, verify_open
from packages.crawler.fetch import build_fetcher
from packages.matching.embed import LexicalEmbedder
from packages.matching.idf import rebuild_if_stale
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

    # Aggregator postings have no board to re-read, so nothing else in the
    # system would ever notice they had closed.
    if payload.get("verify", True):
        report.verified, report.closed_stale = await verify_open(
            session, build_fetcher(), limit=int(payload.get("verify_limit", 50))
        )

    if not report.new_postings:
        return

    postings = list(
        (await session.scalars(select(Posting).where(Posting.closed_at.is_(None)))).all()
    )
    # Statistics first: the embedder is weighted by them, and a vector
    # stamped with the wrong revision is one this pass has to redo.
    texts = [f"{p.title or ''}\n{p.description_raw or ''}" for p in postings]
    frequencies, revision = await rebuild_if_stale(session, texts)
    embedder = LexicalEmbedder(frequencies=frequencies) if frequencies.usable else None
    await embed_postings(session, postings, embedder=embedder, revision=revision)

    for profile in (await session.scalars(select(Profile))).all():
        await score_and_store(session, profile, postings)
