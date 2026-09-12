"""Crawl handler — poll the registry, then score what came back.

Runs as a queue task so it shares the worker's lease machinery: a crawl cycle
over 50 companies at a 60s floor takes at least 50 minutes, which is far
longer than the default lease. Lease renewal (apps/worker/run.py) is what
keeps another worker from reclaiming the task mid-cycle.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config import get_settings
from packages.core.models import QueueTask
from packages.core.queue import ClaimedTask, enqueue
from packages.crawler.crawl import CrawlReport, crawl_all
from packages.crawler.dispatch import tick
from packages.crawler.extract import load_seed
from packages.crawler.fetch import build_fetcher
from packages.crawler.runs import finish_run, start_run, state_recorder
from packages.matching.incremental import run_matching_pass

log = structlog.get_logger(__name__)

CRAWL_TASK_KIND = "crawl"

#: Mirrors `ck_crawl_runs_trigger`. `forced` is set from the payload's `force`
#: flag rather than named directly, so it is not listed as something a caller
#: asks for by name.
VALID_TRIGGERS = frozenset({"scheduled", "manual"})


async def _schedule_next_tick(session: AsyncSession, payload: dict, seconds: int) -> None:
    """Enqueue the next sweep, unless one is already waiting.

    The dispatching cycle reschedules itself rather than being driven by a
    timer thread: the queue already survives restarts, already has a
    `run_after`, and a task row is a schedule anyone can read.

    The guard is what stops it multiplying. Every tick that enqueued a
    successor unconditionally would double the number of ticks each time two
    ever ran at once — and at-least-once delivery means two eventually will.
    A single pending sweep is all this ever needs.
    """
    waiting = await session.scalar(
        select(func.count())
        .select_from(QueueTask)
        .where(
            QueueTask.kind == CRAWL_TASK_KIND,
            QueueTask.status == "pending",
            QueueTask.payload_json["dispatch"].astext == "true",
        )
    )
    if waiting:
        return
    await enqueue(
        session,
        CRAWL_TASK_KIND,
        {**payload, "force": False, "trigger": "scheduled"},
        run_after=datetime.now(UTC) + timedelta(seconds=seconds),
    )


async def handle_crawl(session: AsyncSession, claimed: ClaimedTask) -> None:
    """Run one crawl cycle and score the postings it produced.

    Idempotent: change detection means a repeat run emits nothing, and Match
    rows are upserted rather than appended.
    """
    payload = claimed.task.payload_json or {}
    seed_path = payload.get("seed_path")
    force = bool(payload.get("force"))

    seeds = load_seed(seed_path)
    if not seeds and not payload.get("dispatch"):
        # The dispatching path reads the registry from the database, so an
        # empty seed file is not its problem.
        log.warning("crawl_no_seeds", seed_path=seed_path)
        return

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

    if payload.get("dispatch"):
        # One task per company instead of one loop over all of them. The run
        # row is opened by the tick and closed by whichever company task
        # reports last — no process sees the whole cycle any more.
        settings = get_settings()
        report = await tick(
            session,
            limit=int(payload.get("limit", settings.crawler_dispatch_batch)),
            max_backlog=int(payload.get("max_backlog", settings.crawler_max_backlog)),
            force=force,
            trigger=trigger,
        )
        if report is not None:
            log.info("crawl_dispatch_done", summary=report.summary())

        # The sweep is where scoring happens under dispatch. A per-company
        # task cannot do it — scoring wants the corpus, and 3,500 of them
        # would rebuild the statistics 3,500 times a cycle, each invalidating
        # the vectors the last had written. The sweep runs every few minutes
        # and picks up whatever the finished company tasks left outstanding,
        # which it works out for itself rather than being handed.
        if payload.get("match", True):
            matching = await run_matching_pass(session)
            log.info("crawl_matching", summary=matching.summary())

        if payload.get("repeat", True):
            await _schedule_next_tick(session, payload, settings.crawler_tick_seconds)
        return

    run = await start_run(session, trigger=trigger, companies_total=len(seeds))
    # Held by the caller so a cycle that dies partway still reports what it
    # managed to poll.
    report = CrawlReport()
    try:
        # One fetcher, so one connection pool, for the whole cycle. Closed on
        # the way out either way — a worker runs cycle after cycle, and a pool
        # left open per cycle is a socket leak with a schedule.
        async with build_fetcher() as fetcher:
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

    matching = await run_matching_pass(session)
    log.info("crawl_matching", summary=matching.summary())
