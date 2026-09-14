"""Versions recorded by the crawler, grouping of copies, and the history route.

Uses the real crawl path — `crawl_company` over a mocked board — so what is
asserted is what a crawl leaves behind, not what a helper would.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from packages.core.models import Application, Company, Match, Posting
from packages.core.models_jobs import PostingVersion
from packages.crawler.crawl import crawl_company
from packages.crawler.extract import CompanySeed
from packages.crawler.fetch import PoliteFetcher
from packages.crawler.ratelimit import HostRateLimiter
from packages.matching.canonical import assign

PAY = "<p>Requirements</p><ul><li>Python</li></ul><p>The annual salary range is {pay} USD.</p>"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _fetcher(content: str) -> PoliteFetcher:
    board = {
        "jobs": [
            {
                "id": 1,
                "title": "Backend Engineer",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                "location": {"name": "Remote"},
                "content": content,
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        return httpx.Response(200, text=json.dumps(board))

    clock = _Clock()
    return PoliteFetcher(
        transport=httpx.MockTransport(handler),
        rate_limiter=HostRateLimiter(clock=clock, sleeper=clock.sleep),
    )


SEED = CompanySeed(name="Acme", slug="acme", poll_interval_s=0)


async def _versions(session) -> list[PostingVersion]:
    rows = await session.scalars(select(PostingVersion).order_by(PostingVersion.version))
    return list(rows.all())


# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------


async def test_a_new_posting_is_version_one_and_a_quiet_crawl_adds_nothing(db_session) -> None:
    content = PAY.format(pay="$150,000 - $190,000")
    await crawl_company(db_session, SEED, _fetcher(content), force=True)
    await crawl_company(db_session, SEED, _fetcher(content), force=True)

    assert [v.version for v in await _versions(db_session)] == [1]


async def test_an_edit_is_a_new_version_with_its_pay(db_session) -> None:
    await crawl_company(
        db_session, SEED, _fetcher(PAY.format(pay="$150,000 - $190,000")), force=True
    )
    await crawl_company(
        db_session, SEED, _fetcher(PAY.format(pay="$140,000 - $170,000")), force=True
    )

    versions = await _versions(db_session)
    assert [(v.version, v.salary_max) for v in versions] == [(1, 190_000), (2, 170_000)]


async def test_a_posting_stored_before_versions_gets_a_baseline_on_first_change(
    db_session,
) -> None:
    await crawl_company(
        db_session, SEED, _fetcher(PAY.format(pay="$150,000 - $190,000")), force=True
    )
    # As if stored by an earlier release: the row exists, no version does.
    await db_session.execute(PostingVersion.__table__.delete())

    await crawl_company(
        db_session, SEED, _fetcher(PAY.format(pay="$140,000 - $170,000")), force=True
    )

    versions = await _versions(db_session)
    assert [(v.version, v.salary_max) for v in versions] == [(1, 190_000), (2, 170_000)]


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------

BODY = (
    "We are hiring a backend engineer to build payment infrastructure. You will design "
    "APIs, operate PostgreSQL at scale, and mentor engineers on reliability practices. "
    "Requirements: Python, distributed systems experience, and on-call ownership."
)


async def _company_with_copies(session, *, same_board: bool = False) -> tuple[Posting, Posting]:
    company = Company(name=f"Copies {uuid.uuid4().hex[:6]}")
    session.add(company)
    await session.flush()
    board = Posting(
        company_id=company.id,
        external_id="1",
        ats_type="greenhouse",
        url="https://boards.greenhouse.io/copies/jobs/1",
        title="Backend Engineer",
        location="Remote",
        description_raw=BODY,
    )
    copy = Posting(
        company_id=company.id,
        external_id="2" if same_board else "careers-1",
        ats_type="greenhouse" if same_board else "jsonld",
        url=(
            "https://boards.greenhouse.io/copies/jobs/2"
            if same_board
            else "https://copies.example/careers/backend-engineer"
        ),
        title="Backend Engineer",
        location="Remote",
        description_raw=BODY,
    )
    session.add_all([board, copy])
    await session.flush()
    return board, copy


async def test_copies_on_two_sources_are_grouped(db_session) -> None:
    board, copy = await _company_with_copies(db_session)

    report = await assign(db_session, [board.id, copy.id])

    assert report.groups_created == 1
    assert board.canonical_job_id is not None
    assert board.canonical_job_id == copy.canonical_job_id


async def test_two_listings_on_one_board_stay_apart(db_session) -> None:
    board, other = await _company_with_copies(db_session, same_board=True)

    await assign(db_session, [board.id, other.id])

    assert board.canonical_job_id is None
    assert other.canonical_job_id is None


@pytest.fixture
async def grouped(client: AsyncClient, worker_session, complete_candidate) -> dict:
    board, copy = await _company_with_copies(worker_session)
    await assign(worker_session, [board.id, copy.id])
    profile_id = uuid.UUID(complete_candidate["profile_id"])
    worker_session.add(
        Match(profile_id=profile_id, posting_id=copy.id, score=0.5, decision="skipped")
    )
    await worker_session.commit()
    return {"board": str(board.id), "copy": str(copy.id), "copy_url": copy.url}


async def test_history_lists_every_source_with_its_own_decision(client, grouped) -> None:
    body = (await client.get(f"/postings/{grouped['board']}/history")).json()

    by_id = {source["posting_id"]: source for source in body["sources"]}
    assert set(by_id) == {grouped["board"], grouped["copy"]}
    assert by_id[grouped["copy"]]["decisions"] == ["skipped"]
    assert "words shared" in (
        by_id[grouped["copy"]]["evidence"] or by_id[grouped["board"]]["evidence"]
    )


async def test_splitting_is_permanent(client, grouped, committing_sessionmaker) -> None:
    response = await client.post(f"/postings/{grouped['copy']}/split")

    assert response.status_code == 200
    assert response.json()["locked"] is True
    assert response.json()["canonical_job_id"] is None

    async with committing_sessionmaker() as session:
        await assign(session, [uuid.UUID(grouped["copy"]), uuid.UUID(grouped["board"])])
        await session.commit()
        copy = await session.get(Posting, uuid.UUID(grouped["copy"]))
    assert copy.canonical_job_id is None, "the next grouping pass does not undo the split"


async def test_grouping_touches_no_match_or_application_row(db_session) -> None:
    board, copy = await _company_with_copies(db_session)
    before = (
        await db_session.scalar(select(func.count()).select_from(Match)),
        await db_session.scalar(select(func.count()).select_from(Application)),
    )

    await assign(db_session, [board.id, copy.id])

    after = (
        await db_session.scalar(select(func.count()).select_from(Match)),
        await db_session.scalar(select(func.count()).select_from(Application)),
    )
    assert before == after
