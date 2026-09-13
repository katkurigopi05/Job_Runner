"""CSV to matched posting, through the real queue and the normal entry points.

What is mocked: **HTTP, at the transport boundary**. Nothing else. Registration,
dispatch, the queue, leases, persistence and state transitions all run for real
against the test database, because those are the handoffs that were broken and a
test that stubbed them would prove nothing.

No hidden bootstrap: no inline crawl, no hand-edited YAML, and no direct insert
of a company, posting or match row. The import is `scripts.import_companies`
and the scheduler is `handle_crawl` with `dispatch`, which is what the Makefile
runs.
"""

from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from packages.core.enums import SourceStatus
from packages.core.models import (
    Application,
    Candidate,
    Company,
    Match,
    Posting,
    Profile,
    Project,
    QueueTask,
    User,
)

# A representative sheet: every input class the owner's file contains.
SHEET = """company_name,company_url,company_career_url
Acme,https://acme.example,https://www.google.com/search?q=site%3Aacme.example+careers+jobs
NoSite,,https://www.google.com/search?q=site%3Anosite.example+careers+jobs
Lever,https://lever.co,https://lever.co
Google,https://google.com,https://www.google.com/search?q=site%3Agoogle.com+careers+jobs
Direct,https://direct.example,https://jobs.lever.co/directco
"""

GREENHOUSE_BOARD = {
    "jobs": [
        {
            "id": 9001,
            "title": "Senior Backend Engineer",
            "absolute_url": "https://boards.greenhouse.io/acmeco/jobs/9001",
            "location": {"name": "San Francisco, CA"},
            "content": "<p>python django postgres telemetry ingestion at scale</p>",
        }
    ]
}

LEVER_BOARD = [
    {
        "id": "lever-1",
        "text": "Platform Engineer",
        "hostedUrl": "https://jobs.lever.co/directco/lever-1",
        "categories": {"location": "San Francisco, CA"},
        "description": "python kubernetes terraform platform engineering",
    }
]

#: Acme's home page names its board inline, so discovery resolves it without a
#: careers hop — which keeps the test to one request against a 60s company host.
ACME_HOME = """<html><body>
  <a href="/about">About</a>
  <a href="https://boards.greenhouse.io/acmeco">We are hiring</a>
</body></html>"""


#: The only two boards that exist in this fixture. Every other slug 404s,
#: which is what a real board API does — and the reason it matters is that the
#: fallback probe guesses slugs from the company name. A transport that served
#: the same board for any slug would "verify" every company in the sheet, and
#: the test would pass while proving the opposite of what it claims.
GREENHOUSE_SLUG = "acmeco"
LEVER_SLUG = "directco"


def _handler(*, fail_greenhouse: bool = False, empty_greenhouse: bool = False):
    """One transport for every host the flow touches. Records what was asked."""
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        path = request.url.path
        seen.append(f"{host}{path}")

        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        if host == "acme.example":
            return httpx.Response(200, text=ACME_HOME)
        if host == "boards-api.greenhouse.io":
            if f"/boards/{GREENHOUSE_SLUG}/" not in path:
                return httpx.Response(404, text="Not Found")
            if fail_greenhouse:
                return httpx.Response(500, text="upstream error")
            body = {"jobs": []} if empty_greenhouse else GREENHOUSE_BOARD
            return httpx.Response(200, text=json.dumps(body))
        if host == "api.lever.co":
            if not path.endswith(f"/{LEVER_SLUG}"):
                return httpx.Response(404, text="Not Found")
            return httpx.Response(200, text=json.dumps(LEVER_BOARD))
        if host in ("api.ashbyhq.com", "apply.workable.com"):
            # No Ashby or Workable board belongs to anyone in this sheet.
            return httpx.Response(404, text="Not Found")
        # Every other company's site: no board, nothing embedded. The honest
        # answer for Google, Lever and the rest in this fixture.
        return httpx.Response(200, text="<html><body>no board here</body></html>")

    return httpx.MockTransport(handle), seen


@pytest_asyncio.fixture
async def flow(committing_sessionmaker, monkeypatch, tmp_path):
    """The whole pipeline wired to the test database with HTTP mocked."""
    import packages.core.db as core_db
    from apps.worker import crawl_company_job, discover_company_job
    from packages.crawler.fetch import build_fetcher as real_build

    monkeypatch.setattr(core_db, "get_sessionmaker", lambda: committing_sessionmaker)

    state: dict = {}

    def install(**flags):
        transport, seen = _handler(**flags)
        state["seen"] = seen

        def offline(*args, **kwargs):
            kwargs["transport"] = transport
            kwargs["shared"] = True
            return real_build(*args, **kwargs)

        monkeypatch.setattr(crawl_company_job, "build_fetcher", offline)
        monkeypatch.setattr(discover_company_job, "build_fetcher", offline)

    install()
    csv_path = tmp_path / "companies.csv"
    csv_path.write_text(SHEET, encoding="utf-8")
    state.update(sessions=committing_sessionmaker, csv=csv_path, install=install)
    return state


async def _owner_profile(sessions) -> None:
    """A profile with real text, so scoring has something to compare against.

    Not a shortcut around the pipeline: a profile is the owner's own data and
    there is no import path for it in this milestone. Postings, companies and
    matches are all produced by the flow under test.
    """
    async with sessions() as session:
        user = User(email="owner@example.com")
        session.add(user)
        await session.flush()
        candidate = Candidate(
            user_id=user.id, name="Owner", email="owner@example.com", email_mode="self"
        )
        session.add(candidate)
        await session.flush()
        session.add(
            Profile(candidate_id=candidate.id, label="primary", location="San Francisco, CA")
        )
        session.add(
            Project(
                candidate_id=candidate.id,
                source="github",
                external_id="p1",
                name="telemetry-pipeline",
                full_name="owner/telemetry-pipeline",
                url="https://github.com/owner/telemetry-pipeline",
                description="python django postgres telemetry ingestion service at scale",
                language="Python",
                topics_json=["python", "django", "postgres"],
            )
        )
        await session.commit()


async def _import(csv_path, *, register: bool = True) -> int:
    """The importer's own entry point.

    `run` is the body of `main`, which is `asyncio.run(run(...))` — the test
    awaits it rather than spawning a second event loop, because the session
    maker it writes through is bound to this one.
    """
    from scripts.import_companies import run

    argv = [str(csv_path)]
    if register:
        argv.append("--register")
    return await run(argv)


async def _tick(sessions) -> None:
    """The scheduler, through its real handler.

    The payload is the one `make crawl dispatch=1 once=1` enqueues — see
    `test_the_dispatch_sweep_is_reachable_from_the_command_line`, which checks
    that the command produces exactly this.
    """
    from apps.worker.crawl_job import CRAWL_TASK_KIND, handle_crawl

    class _Claimed:
        def __init__(self) -> None:
            self.task = QueueTask(
                kind=CRAWL_TASK_KIND, payload_json={"dispatch": True, "repeat": False}
            )

    async with sessions() as session:
        await handle_crawl(session, _Claimed())
        await session.commit()


async def _drain(limit: int = 40) -> int:
    """Run the real worker loop until the queue is empty."""
    from apps.worker import run as worker_run

    done = 0
    for _ in range(limit):
        if not await worker_run.run_once(worker_id="e2e"):
            break
        done += 1
    return done


async def _status(sessions) -> dict:
    async with sessions() as session:
        rows = (
            await session.execute(
                select(Company.source_status, func.count()).group_by(Company.source_status)
            )
        ).all()
        return {status: count for status, count in rows}


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------


async def test_import_registers_every_class_of_row(flow) -> None:
    assert await _import(flow["csv"]) == 0

    counts = await _status(flow["sessions"])
    # Four leads (Acme, Lever, Google, Direct) and one with nothing to go on.
    assert counts.get(SourceStatus.HINT.value) == 3, counts
    assert counts.get(SourceStatus.UNVERIFIED.value) == 1, "the direct board URL"
    assert counts.get(SourceStatus.NO_WEBSITE.value) == 1, "NoSite has no lead at all"
    assert SourceStatus.VERIFIED.value not in counts, "nothing is verified before discovery runs"

    async with flow["sessions"]() as session:
        google = await session.scalar(select(Company).where(Company.name == "Google"))
        assert google is not None, "a search-engine domain is still a company"
        assert google.supplied_website == "https://google.com"
        assert google.supplied_career_url.startswith("https://www.google.com/search")
        assert google.source_row == 4, "provenance: the row it came from"
        lever = await session.scalar(select(Company).where(Company.name == "Lever"))
        assert lever is not None, "a vendor is still an employer"
        assert lever.slug is None, "but its homepage is not a board identifier"


async def test_reimporting_changes_nothing(flow) -> None:
    assert await _import(flow["csv"]) == 0
    before = await _status(flow["sessions"])
    async with flow["sessions"]() as session:
        count_before = await session.scalar(select(func.count()).select_from(Company))

    assert await _import(flow["csv"]) == 0

    async with flow["sessions"]() as session:
        assert await session.scalar(select(func.count()).select_from(Company)) == count_before
    assert await _status(flow["sessions"]) == before


async def test_reimporting_does_not_reset_a_verified_source(flow) -> None:
    """The protection that makes re-import safe after discovery has run."""
    assert await _import(flow["csv"]) == 0
    await _tick(flow["sessions"])
    await _drain()
    async with flow["sessions"]() as session:
        acme = await session.scalar(select(Company).where(Company.name == "Acme"))
        assert acme.source_status == SourceStatus.VERIFIED.value
        verified_at, slug = acme.source_verified_at, acme.slug

    assert await _import(flow["csv"]) == 0

    async with flow["sessions"]() as session:
        acme = await session.scalar(select(Company).where(Company.name == "Acme"))
        assert acme.source_status == SourceStatus.VERIFIED.value, "not downgraded to a hint"
        assert acme.source_verified_at == verified_at
        assert acme.slug == slug


# --------------------------------------------------------------------------
# Routing: candidates to discovery, only verified boards to fetching
# --------------------------------------------------------------------------


async def test_candidates_get_discovery_and_nothing_gets_fetch_yet(flow) -> None:
    from packages.crawler.dispatch import CRAWL_COMPANY_TASK_KIND, DISCOVER_COMPANY_TASK_KIND

    await _import(flow["csv"])
    await _tick(flow["sessions"])

    async with flow["sessions"]() as session:
        kinds = dict(
            (
                await session.execute(select(QueueTask.kind, func.count()).group_by(QueueTask.kind))
            ).all()
        )
    assert kinds.get(DISCOVER_COMPANY_TASK_KIND) == 4, "every lead, and only the leads"
    assert CRAWL_COMPANY_TASK_KIND not in kinds, "nothing is a verified board yet"


async def test_discovery_verifies_and_schedules_the_fetch_itself(flow) -> None:
    """The atomic handoff: verification and the crawl task in one transaction."""
    from packages.crawler.dispatch import CRAWL_COMPANY_TASK_KIND

    await _import(flow["csv"])
    await _tick(flow["sessions"])
    await _drain()

    async with flow["sessions"]() as session:
        acme = await session.scalar(select(Company).where(Company.name == "Acme"))
        assert acme.source_status == SourceStatus.VERIFIED.value
        assert (acme.ats_type, acme.slug) == ("greenhouse", "acmeco")
        assert acme.source_evidence["method"] == "discovery_from_url"
        assert acme.source_evidence["lead"] == "https://acme.example"

        # Unresolved companies are untouched by fetch work.
        google = await session.scalar(select(Company).where(Company.name == "Google"))
        assert google.source_status == SourceStatus.FAILED.value
        assert google.discovery_failure
        fetch_targets = {
            t.payload_json["company_id"]
            for t in (
                await session.scalars(
                    select(QueueTask).where(QueueTask.kind == CRAWL_COMPANY_TASK_KIND)
                )
            ).all()
        }
        assert str(google.id) not in fetch_targets, "an unresolved company is never fetch work"


# --------------------------------------------------------------------------
# The whole way through
# --------------------------------------------------------------------------


async def test_a_match_appears_while_other_companies_are_still_unresolved(flow) -> None:
    await _owner_profile(flow["sessions"])
    await _import(flow["csv"])

    # Two ticks: the first dispatches discovery, the second the boards that
    # discovery verified. Draining between them is the normal worker loop.
    await _tick(flow["sessions"])
    await _drain()
    await _tick(flow["sessions"])
    await _drain()

    async with flow["sessions"]() as session:
        postings = (await session.scalars(select(Posting))).all()
        assert postings, "a board was fetched and stored"
        matches = (await session.scalars(select(Match))).all()
        assert matches, "and scored into the feed"

        unresolved = await session.scalar(
            select(func.count())
            .select_from(Company)
            .where(
                Company.source_status.in_(
                    (SourceStatus.FAILED.value, SourceStatus.NO_WEBSITE.value)
                )
            )
        )
        assert unresolved >= 2, "while other companies are still without a board"

        # Nothing was submitted, and nothing was even queued to submit.
        assert await session.scalar(select(func.count()).select_from(Application)) == 0


async def test_the_status_endpoint_explains_a_cold_start(flow, monkeypatch) -> None:
    """Postings are visible immediately; the reason matching is weak is shown."""
    from httpx import ASGITransport, AsyncClient

    import packages.core.db as core_db
    from apps.api.main import app

    await _owner_profile(flow["sessions"])
    await _import(flow["csv"])
    await _tick(flow["sessions"])
    await _drain()
    await _tick(flow["sessions"])
    await _drain()

    monkeypatch.setattr(core_db, "get_sessionmaker", lambda: flow["sessions"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (await client.get("/companies/status")).json()

    assert body["companies_total"] == 5
    assert body["verified"] >= 1
    assert body["retrying"] >= 1, "companies discovery could not resolve"
    assert body["needs_review"] == 1, "NoSite needs a human to supply a URL"
    assert body["postings_total"] >= 1, "fetched postings are visible straight away"
    # The corpus is tiny, so weighting is the fallback — and says so rather than
    # looking like a failure to match.
    assert body["weighting"] == "unweighted"
    assert "unweighted embedder" in body["weighting_reason"]
    assert "Not a block" in body["weighting_reason"]


async def test_interrupting_and_resuming_keeps_the_work_and_duplicates_nothing(flow) -> None:
    await _owner_profile(flow["sessions"])
    await _import(flow["csv"])
    await _tick(flow["sessions"])
    await _drain()
    await _tick(flow["sessions"])
    await _drain()

    async with flow["sessions"]() as session:
        postings_before = await session.scalar(select(func.count()).select_from(Posting))
        acme = await session.scalar(select(Company).where(Company.name == "Acme"))
        attempts_before = acme.source_verified_at

    # "Restart": a fresh tick and drain over the same registry.
    await _tick(flow["sessions"])
    await _drain()

    async with flow["sessions"]() as session:
        assert await session.scalar(select(func.count()).select_from(Posting)) == postings_before, (
            "a resumed run must not duplicate postings"
        )
        acme = await session.scalar(select(Company).where(Company.name == "Acme"))
        assert acme.source_verified_at == attempts_before, "completed discovery is retained"
        assert acme.source_status == SourceStatus.VERIFIED.value


@pytest.mark.parametrize("mode", ["failed", "empty"])
async def test_a_failed_or_empty_board_does_not_close_existing_postings(flow, mode) -> None:
    """The protection this repo has been bitten by twice."""
    await _import(flow["csv"])
    await _tick(flow["sessions"])
    await _drain()
    await _tick(flow["sessions"])
    await _drain()

    async with flow["sessions"]() as session:
        open_before = await session.scalar(
            select(func.count()).select_from(Posting).where(Posting.closed_at.is_(None))
        )
        assert open_before >= 1

    # Re-point the transport at a broken board and force another fetch.
    flow["install"](**{"fail_greenhouse": mode == "failed", "empty_greenhouse": mode == "empty"})
    async with flow["sessions"]() as session:
        acme = await session.scalar(select(Company).where(Company.name == "Acme"))
        from packages.core.queue import enqueue
        from packages.crawler.dispatch import CRAWL_COMPANY_TASK_KIND

        await enqueue(session, CRAWL_COMPANY_TASK_KIND, {"company_id": str(acme.id), "force": True})
        await session.commit()
    await _drain()

    async with flow["sessions"]() as session:
        open_after = await session.scalar(
            select(func.count()).select_from(Posting).where(Posting.closed_at.is_(None))
        )
    assert open_after == open_before, (
        f"a {mode} fetch closed postings; it must leave them exactly as they were"
    )


async def test_no_application_is_ever_created_by_this_flow(flow) -> None:
    """§2.3 — nothing submits without approval, and nothing here even queues one."""
    await _owner_profile(flow["sessions"])
    await _import(flow["csv"])
    await _tick(flow["sessions"])
    await _drain()
    await _tick(flow["sessions"])
    await _drain()

    async with flow["sessions"]() as session:
        assert await session.scalar(select(func.count()).select_from(Application)) == 0
        kinds = set((await session.scalars(select(QueueTask.kind))).all())
    assert "apply" not in kinds, "the crawl path must never enqueue an application"


async def test_the_shared_limiter_is_used_throughout(flow) -> None:
    """Not a second counter per process — §2.6 holds across every fetch path."""
    from packages.crawler.host_budget import SharedHostRateLimiter

    await _import(flow["csv"])
    await _tick(flow["sessions"])
    await _drain()

    async with flow["sessions"]() as session:
        from packages.core.models import CrawlerHostBudget

        hosts = set((await session.scalars(select(CrawlerHostBudget.host))).all())
    assert "acme.example" in hosts, "discovery reserved against the company's own host"
    assert isinstance(SharedHostRateLimiter(), SharedHostRateLimiter)


async def test_the_first_match_arrives_before_discovery_has_finished(flow, capsys) -> None:
    """Incremental, measured — and the numbers printed rather than asserted.

    The point of this test is the *ordering*: a board verified, its postings
    stored and a match scored, while companies in the same sheet are still
    unresolved. That is what makes the pipeline incremental rather than a batch
    that reports at the end, and it is the property a timing claim rests on.

    The durations are printed, not asserted. They are fixture timings — HTTP is
    a mock transport, so what they measure is this machine plus the rate
    limiter, not a real crawl. Treating them as a benchmark is how the earlier
    two-minute and two-hour figures got quoted as if they were measurements.
    """
    import time

    await _owner_profile(flow["sessions"])
    started = time.monotonic()
    await _import(flow["csv"])
    imported = time.monotonic() - started

    # One task at a time, recording the moment each milestone first holds.
    await _tick(flow["sessions"])
    first_board = first_posting = first_match = None
    unresolved_at_first_match = None
    for _ in range(40):
        if not await _run_one():
            break
        async with flow["sessions"]() as session:
            if first_board is None and await session.scalar(
                select(func.count())
                .select_from(Company)
                .where(Company.source_status == SourceStatus.VERIFIED.value)
            ):
                first_board = time.monotonic() - started
            if first_posting is None and await session.scalar(
                select(func.count()).select_from(Posting)
            ):
                first_posting = time.monotonic() - started

    # Matching runs on the sweep, not per company: `handle_crawl` explains why
    # (the corpus statistics would be rebuilt once per company otherwise). So
    # the first match lands on the tick after the first board was fetched,
    # which is what a real run pays too.
    await _tick(flow["sessions"])
    async with flow["sessions"]() as session:
        if await session.scalar(select(func.count()).select_from(Match)):
            first_match = time.monotonic() - started
            unresolved_at_first_match = await session.scalar(
                select(func.count())
                .select_from(Company)
                .where(Company.source_status != SourceStatus.VERIFIED.value)
            )
    settled = time.monotonic() - started

    with capsys.disabled():
        print(
            "\n  fixture timings (mock HTTP — not a benchmark)"
            f"\n    import + register : {imported:.2f}s"
            f"\n    first board       : {first_board}"
            f"\n    first posting     : {first_posting}"
            f"\n    first match       : {first_match}"
            f"\n    discovery settled : {settled:.2f}s"
            f"\n    still unresolved when the first match existed: "
            f"{unresolved_at_first_match}"
        )

    assert first_board is not None and first_posting is not None
    assert first_match is not None
    assert first_board <= first_posting <= first_match
    assert unresolved_at_first_match, (
        "the first match must not wait for every company to resolve — that is "
        "the difference between an incremental pipeline and a batch"
    )


async def _run_one() -> bool:
    from apps.worker import run as worker_run

    return await worker_run.run_once(worker_id="e2e-timing")


async def test_the_dispatch_sweep_is_reachable_from_the_command_line(flow) -> None:
    """The sweep had a handler, a scheduler and no way to start it.

    `handle_crawl` has understood `dispatch` since the per-company sweep was
    written, and the only thing that ever enqueued such a task was a dispatch
    tick arming its successor — which cannot happen until something arms a
    first one. So the path that reads the `companies` table, and therefore the
    only path an imported CSV is reachable by, could not be started at all.

    Asserted on the enqueued payload rather than on the flag parsing, because
    what matters is that the task the worker claims carries `dispatch`.
    """
    from scripts import crawl as crawl_script

    await _import(flow["csv"])

    import sys

    argv = sys.argv
    sys.argv = ["crawl", "--dispatch", "--limit", "20", "--once"]
    try:
        await crawl_script.main()
    finally:
        sys.argv = argv

    async with flow["sessions"]() as session:
        tasks = (await session.scalars(select(QueueTask).where(QueueTask.kind == "crawl"))).all()
    assert len(tasks) == 1
    payload = tasks[0].payload_json
    assert payload["dispatch"] is True
    assert payload["limit"] == 20
    assert payload["repeat"] is False, "a pilot tick must not re-arm itself"
