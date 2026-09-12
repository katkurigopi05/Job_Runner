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
from packages.crawler.crawl import CrawlReport, crawl_all
from packages.crawler.extract import load_seed
from packages.crawler.fetch import build_fetcher
from packages.crawler.runs import finish_run, start_run, state_recorder
from packages.matching.embed import LexicalEmbedder
from packages.matching.idf import rebuild_if_stale
from packages.matching.score import embed_postings, score_and_store

log = structlog.get_logger(__name__)

CRAWL_TASK_KIND = "crawl"

#: Mirrors `ck_crawl_runs_trigger`. `forced` is set from the payload's `force`
#: flag rather than named directly, so it is not listed as something a caller
#: asks for by name.
VALID_TRIGGERS = frozenset({"scheduled", "manual"})


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
    # Checked against the column's CHECK constraint rather than passed
    # through: the payload is JSON from whoever enqueued the task, and an
    # unexpected word in it would fail the INSERT and take the whole cycle
    # with it over something purely descriptive.
    trigger = payload.get("trigger", "scheduled")
    if force:
        trigger = "forced"
    elif trigger not in VALID_TRIGGERS:
        log.warning("crawl_unknown_trigger", trigger=trigger)
        trigger = "scheduled"

    run = await start_run(session, trigger=trigger, companies_total=len(seeds))
    # Held by the caller so a cycle that dies partway still reports what it
    # managed to poll.
    report = CrawlReport()
    try:
        await crawl_all(
            session,
            seeds,
            fetcher,
            force=force,
            on_result=state_recorder(session, run),
            report=report,
        )
    except Exception:
        # The run row is the only record that a cycle was attempted at all, so
        # it is closed on the way out rather than left `running` forever —
        # which would otherwise be indistinguishable from a cycle still going.
        #
        # Best effort: if the failure was the database itself, this session is
        # already poisoned and the close cannot land. Losing the run row is a
        # much smaller loss than replacing the real traceback with whatever
        # error this raises on the way past.
        try:
            await finish_run(session, run, report, status="aborted")
        except Exception:  # noqa: BLE001 - never mask the original failure
            log.warning("crawl_run_abort_unrecorded", run_id=str(run.id))
        raise
    await finish_run(session, run, report)
    log.info("crawl_done", summary=report.summary())

    if not report.emitted:
        return

    # Only postings that are actually open are worth embedding or scoring.
    postings = list(
        (await session.scalars(select(Posting).where(Posting.closed_at.is_(None)))).all()
    )
    # Statistics first: the embedder is weighted by them, and a vector
    # stamped with the wrong revision is one this pass has to redo.
    texts = [f"{p.title or ''}\n{p.description_raw or ''}" for p in postings]
    frequencies, revision = await rebuild_if_stale(session, texts)
    embedder = LexicalEmbedder(frequencies=frequencies) if frequencies.usable else None
    embedded = await embed_postings(session, postings, embedder=embedder, revision=revision)
    log.info("postings_embedded", count=embedded)

    profiles = list((await session.scalars(select(Profile))).all())
    for profile in profiles:
        await score_and_store(session, profile, postings)
