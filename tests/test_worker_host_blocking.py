"""One throttled host must not occupy every worker slot.

`run_once` claims a single task and awaits its handler to completion, and
`claim_task` orders by `run_after`. So a handler that sleeps inside
`rate_limiter.acquire()` holds its slot for the whole wait, and work for an
idle host queues behind it. The per-host counters were never the problem — the
shared limiter keeps those correctly separate — it is the pool.

The first test is the **reproduction**: with deferral switched off by raising
its threshold above the wait, a single worker spends the whole penalty on the
throttled company and the idle one does not start. That arm is the measurement
of the defect, taken with the same code and one setting changed, so it cannot
drift away from what the fix is claimed to fix.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from packages.core.enums import SourceStatus
from packages.core.models import Company, Posting, QueueTask
from packages.core.queue import enqueue
from packages.crawler.dispatch import CRAWL_COMPANY_TASK_KIND

GREENHOUSE_HOST = "boards-api.greenhouse.io"
#: Above `defer.DEFAULT_THRESHOLD_S` (3s) so deferral engages, and small enough
#: that the control arm — which really does wait — keeps the suite quick.
PENALTY_S = 4.0


def _board(job_id: int) -> dict:
    return {
        "jobs": [
            {
                "id": job_id,
                "title": "Senior Backend Engineer",
                "absolute_url": f"https://boards.greenhouse.io/x/jobs/{job_id}",
                "location": {"name": "Remote"},
                "content": "<p>Python and Postgres</p>",
            }
        ]
    }


#: Lever answers with a bare JSON array, Greenhouse with `{"jobs": [...]}`.
#: Serving one shape to both hosts made the Lever board parse to nothing, which
#: looked like the scheduling fix having failed.
def _lever_board(job_id: int) -> list[dict]:
    return [
        {
            "id": f"lever-{job_id}",
            "text": "Senior Backend Engineer",
            "hostedUrl": f"https://jobs.lever.co/globex/{job_id}",
            "categories": {"location": "Remote"},
            "description": "Python and Postgres",
        }
    ]


def _transport() -> httpx.MockTransport:
    """Answers robots and either board shape, so the only delay is the limiter."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        if "lever.co" in (request.url.host or ""):
            return httpx.Response(200, text=json.dumps(_lever_board(1)))
        return httpx.Response(200, text=json.dumps(_board(1)))

    return httpx.MockTransport(handler)


@pytest_asyncio.fixture
async def worker(committing_sessionmaker, monkeypatch):
    """A real worker loop against the test database, with HTTP mocked."""
    import packages.core.db as core_db
    from apps.worker import crawl_company_job
    from packages.crawler.fetch import build_fetcher as real_build

    monkeypatch.setattr(core_db, "get_sessionmaker", lambda: committing_sessionmaker)

    def offline(*args, **kwargs):
        kwargs["transport"] = _transport()
        kwargs["shared"] = True
        return real_build(*args, **kwargs)

    monkeypatch.setattr(crawl_company_job, "build_fetcher", offline)
    return committing_sessionmaker


async def _two_companies(sessions) -> tuple[Company, Company]:
    """One Greenhouse board and one Lever board — two different hosts."""
    async with sessions() as session:
        acme = Company(
            name="Acme",
            slug="acme",
            ats_type="greenhouse",
            poll_interval_s=0,
            source_status=SourceStatus.VERIFIED.value,
        )
        globex = Company(
            name="Globex",
            slug="globex",
            ats_type="lever",
            poll_interval_s=0,
            source_status=SourceStatus.VERIFIED.value,
        )
        session.add_all([acme, globex])
        await session.flush()
        now = datetime.now(UTC)
        # Acme is claimed first: FIFO by run_after is what puts the throttled
        # company at the head of the queue.
        await enqueue(
            session,
            CRAWL_COMPANY_TASK_KIND,
            {"company_id": str(acme.id), "force": True},
            run_after=now - timedelta(seconds=10),
        )
        await enqueue(
            session,
            CRAWL_COMPANY_TASK_KIND,
            {"company_id": str(globex.id), "force": True},
            run_after=now - timedelta(seconds=5),
        )
        await session.commit()
        return acme, globex


async def _throttle(sessions, host: str, seconds: float) -> None:
    from packages.crawler.host_budget import SharedHostRateLimiter

    limiter = SharedHostRateLimiter(sessions=sessions)
    await limiter.penalize(host, seconds)


async def test_without_deferral_one_busy_host_holds_the_slot(worker, monkeypatch) -> None:
    """The reproduction. Deferral off, and the worker sleeps in its slot.

    `MAX_DEFERRALS = 0` switches it off without touching the threshold, so the
    only difference from the test below is whether the task may be handed back.
    """
    from packages.crawler import defer

    monkeypatch.setattr(defer, "MAX_DEFERRALS", 0)
    await _two_companies(worker)
    await _throttle(worker, GREENHOUSE_HOST, PENALTY_S)

    from apps.worker import run as worker_run

    started = time.monotonic()
    assert await worker_run.run_once(worker_id="solo") is True
    elapsed = time.monotonic() - started

    assert elapsed >= PENALTY_S * 0.8, (
        f"the worker returned in {elapsed:.2f}s; it was supposed to sit in the penalty"
    )
    async with worker() as session:
        # And Globex — on an idle host — has not run at all.
        assert await session.scalar(select(func.count()).select_from(Posting)) == 1


async def test_a_busy_host_gives_the_slot_back_and_the_idle_host_runs(worker) -> None:
    """The fix. Acme defers in milliseconds and Globex goes first.

    Timestamps, not completion order: a task that slept and then finished first
    has still occupied the pool for the whole wait.
    """
    acme, globex = await _two_companies(worker)
    await _throttle(worker, GREENHOUSE_HOST, PENALTY_S)

    from apps.worker import run as worker_run

    started = time.monotonic()
    assert await worker_run.run_once(worker_id="solo") is True
    deferred_at = time.monotonic() - started
    assert deferred_at < PENALTY_S / 2, (
        f"handing the slot back took {deferred_at:.2f}s — it should be a round trip"
    )

    async with worker() as session:
        task = await session.scalar(
            select(QueueTask).where(QueueTask.payload_json["company_id"].astext == str(acme.id))
        )
        assert task.status == "pending", "deferred, not failed"
        assert task.attempts == 0, "a limiter deferral must not consume a retry"
        assert task.run_after > datetime.now(UTC), "it comes back when the host is free"
        assert task.payload_json["deferrals"] == 1
        assert task.locked_by is None

    # The idle host now runs, while Acme is still waiting.
    assert await worker_run.run_once(worker_id="solo") is True
    finished_globex = time.monotonic() - started
    assert finished_globex < PENALTY_S, (
        f"Globex finished at {finished_globex:.2f}s, after Acme's penalty — it was blocked"
    )

    async with worker() as session:
        postings = (await session.scalars(select(Posting))).all()
        assert len(postings) == 1, "exactly the idle host's board was fetched"
        company_ids = {p.company_id for p in postings}
        assert company_ids == {globex.id}


async def test_deferral_is_bounded_so_nothing_is_postponed_for_ever(worker) -> None:
    """After `MAX_DEFERRALS` the task waits instead of being handed back again.

    A host that is always busy could otherwise return a task for ever: each
    deferral lands it at the moment the host frees, where another worker may
    already be reserving. Someone always progresses, so it is not a deadlock —
    but one company could starve, so the deferrals are counted.
    """
    from packages.crawler import defer

    acme, _ = await _two_companies(worker)
    async with worker() as session:
        task = await session.scalar(
            select(QueueTask).where(QueueTask.payload_json["company_id"].astext == str(acme.id))
        )
        payload = dict(task.payload_json)
        payload["deferrals"] = defer.MAX_DEFERRALS
        task.payload_json = payload
        await session.commit()

    await _throttle(worker, GREENHOUSE_HOST, PENALTY_S)
    from apps.worker import run as worker_run

    started = time.monotonic()
    assert await worker_run.run_once(worker_id="solo") is True
    elapsed = time.monotonic() - started

    assert elapsed >= PENALTY_S * 0.8, "at the bound it must wait, not defer again"
    async with worker() as session:
        assert await session.scalar(select(func.count()).select_from(Posting)) == 1


@pytest.mark.parametrize("waiting", [0.0, 1.0])
async def test_a_short_wait_is_not_worth_a_round_trip(worker, monkeypatch, waiting) -> None:
    """Below the threshold, sleeping beats a deferral.

    The shared ATS floor is 2s. A threshold under it would defer nearly every
    task nearly every time and turn the queue into a spin.
    """
    from packages.crawler import defer

    async def _peek(self, host: str) -> float:
        return waiting

    monkeypatch.setattr("packages.crawler.host_budget.SharedHostRateLimiter.wait_for", _peek)
    acme, _ = await _two_companies(worker)
    from apps.worker import run as worker_run

    assert await worker_run.run_once(worker_id="solo") is True

    async with worker() as session:
        task = await session.scalar(
            select(QueueTask).where(QueueTask.payload_json["company_id"].astext == str(acme.id))
        )
        assert "deferrals" not in (task.payload_json or {})
        assert defer.DEFAULT_THRESHOLD_S > 2.0, "the threshold must clear the shared floor"
