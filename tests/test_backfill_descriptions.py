"""`make backfill-descriptions`: every missing job page, read politely, stored as a crawl would."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from sqlalchemy import select, update

from packages.core.models import Posting
from packages.crawler.backfill import (
    REFUSALS_BEFORE_STOP_SERVING,
    backfill,
    fill_host,
    find_pending,
)
from packages.crawler.crawl import crawl_company
from packages.crawler.extract import CompanySeed
from packages.crawler.fetch import PoliteFetcher
from packages.crawler.ratelimit import MIN_DELAY_SECONDS, HostRateLimiter
from tests.test_workday import HOST, SLUG, FakeClock, _job

SEED = CompanySeed(name="Acme", slug=SLUG, ats="workday", poll_interval_s=0)
ALLOW = "User-agent: *\nDisallow:"


def _board(
    total: int = 9,
    *,
    robots: str = ALLOW,
    gone: frozenset[int] = frozenset(),
    page_status: int = 200,
    refused: frozenset[int] = frozenset(),
):
    """A Workday board whose list and job pages are replayed offline.

    `gone` are job numbers whose page answers 404, as a requisition closed
    between the list and the request does. `page_status` answers every job
    page, as a site that refuses them all does (403 on three tenants, live).
    `refused` are job numbers that alone answer 403, as about one page in
    five did on big tenants that served the rest (2026-11-02).
    """
    calls: list[tuple[str, str, float]] = []
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        calls.append((request.method, request.url.path, clock.now))
        if request.method == "POST":
            offset = json.loads(request.content)["offset"]
            jobs = [_job(n) for n in range(offset, min(total, offset + 20))]
            return httpx.Response(
                200, json={"total": total if offset == 0 else 0, "jobPostings": jobs}
            )
        if page_status != 200:
            return httpx.Response(page_status)
        if int(request.url.path.rsplit("-", 1)[-1]) in gone:
            return httpx.Response(404)
        if int(request.url.path.rsplit("-", 1)[-1]) in refused:
            return httpx.Response(403)
        return httpx.Response(
            200,
            json={
                "jobPostingInfo": {
                    "jobDescription": "<p>Python systems</p>",
                    "startDate": datetime.now(UTC).date().isoformat(),
                }
            },
        )

    fetcher = PoliteFetcher(
        transport=httpx.MockTransport(handler),
        rate_limiter=HostRateLimiter(clock=clock, sleeper=clock.sleep),
    )
    return fetcher, calls


def _sessions(session):
    """One host in these tests, so its one session is safe to hand out."""

    @asynccontextmanager
    async def opened():
        yield session

    return opened


async def _crawled(db_session, total: int = 9) -> None:
    """A first crawl: every posting stored, five of them with their text."""
    fetcher, _ = _board(total)
    result = await crawl_company(db_session, SEED, fetcher, force=True)
    assert result.new_postings == total


async def _descriptions(db_session) -> dict[str, str | None]:
    db_session.expire_all()
    rows = (await db_session.scalars(select(Posting))).all()
    return {row.external_id: row.description_raw for row in rows}


async def test_every_missing_description_is_read_without_reading_the_list(db_session):
    await _crawled(db_session)
    assert sum(text is None for text in (await _descriptions(db_session)).values()) == 4

    pending = await find_pending(db_session)
    assert {host: len(items) for host, items in pending.items()} == {HOST: 4}

    fetcher, calls = _board()
    report = await backfill(fetcher, _sessions(db_session))

    assert report.filled == 4
    assert report.failed == 0
    assert all(text == "Python systems" for text in (await _descriptions(db_session)).values())
    # Job pages only: the list is a crawl's to read, and so is closing.
    assert [method for method, _, _ in calls] == ["GET"] * 4
    # §2.6: one host, one request per floor.
    assert all(b[2] - a[2] >= MIN_DELAY_SECONDS for a, b in zip(calls, calls[1:], strict=False))


async def test_a_page_that_fails_leaves_that_posting_waiting_and_open(db_session):
    await _crawled(db_session)
    waiting = [eid for eid, text in (await _descriptions(db_session)).items() if text is None]
    gone = int(waiting[0].rsplit("-", 1)[-1])

    fetcher, calls = _board(gone=frozenset({gone}))
    report = await backfill(fetcher, _sessions(db_session))

    assert report.filled == 3
    assert report.failed == 1
    assert len(calls) == 4
    row = await db_session.scalar(select(Posting).where(Posting.external_id == waiting[0]))
    assert row.description_raw is None
    assert row.closed_at is None


async def test_robots_refusing_job_pages_stops_that_host(db_session):
    await _crawled(db_session)

    fetcher, calls = _board(robots="User-agent: *\nDisallow: /wday/cxs/")
    report = await backfill(fetcher, _sessions(db_session))

    assert report.filled == 0
    assert report.blocked_hosts == [HOST]
    assert calls == []


async def test_a_host_that_keeps_refusing_is_left_alone(db_session):
    """§2.5: a site saying no is not asked again a minute later, page after page."""
    await _crawled(db_session, total=12)  # five read, seven waiting

    fetcher, calls = _board(total=12, page_status=403)
    report = await backfill(fetcher, _sessions(db_session))

    assert report.refused_hosts == [HOST]
    assert report.filled == 0
    assert len(calls) == 3
    assert sum(text is None for text in (await _descriptions(db_session)).values()) == 7


async def test_one_missing_page_does_not_stop_the_host(db_session):
    """A 404 is one job gone, not the site refusing; the rest are still read."""
    await _crawled(db_session, total=12)
    waiting = sorted(eid for eid, text in (await _descriptions(db_session)).items() if not text)
    gone = frozenset(int(eid.rsplit("-", 1)[-1]) for eid in waiting[:4])

    fetcher, calls = _board(total=12, gone=gone)
    report = await backfill(fetcher, _sessions(db_session))

    assert report.refused_hosts == []
    assert len(calls) == 7
    assert report.filled == 3


async def test_a_posting_closed_since_the_run_began_is_not_reopened(db_session):
    """`_store` writes `closed_at = NULL`; a crawl's closure must outlast a stale list."""
    await _crawled(db_session)
    pending = await find_pending(db_session)
    closed = pending[HOST][0].listing.external_id
    await db_session.execute(
        update(Posting).where(Posting.external_id == closed).values(closed_at=datetime.now(UTC))
    )

    fetcher, _ = _board()
    report = await fill_host(fetcher, db_session, HOST, pending[HOST])

    assert report.skipped == 1
    assert report.filled == 3
    row = await db_session.scalar(
        select(Posting)
        .where(Posting.external_id == closed)
        .execution_options(populate_existing=True)
    )
    assert row.closed_at is not None
    assert row.description_raw is None


async def test_a_posting_a_crawl_filled_meanwhile_keeps_the_crawls_text(db_session):
    await _crawled(db_session)
    pending = await find_pending(db_session)
    filled = pending[HOST][0].listing.external_id
    await db_session.execute(
        update(Posting).where(Posting.external_id == filled).values(description_raw="From a crawl")
    )

    fetcher, _ = _board()
    report = await fill_host(fetcher, db_session, HOST, pending[HOST])

    assert report.skipped == 1
    row = await db_session.scalar(
        select(Posting)
        .where(Posting.external_id == filled)
        .execution_options(populate_existing=True)
    )
    assert row.description_raw == "From a crawl"


async def test_a_limit_caps_the_requests_per_host(db_session):
    await _crawled(db_session)

    fetcher, calls = _board()
    report = await backfill(fetcher, _sessions(db_session), limit=2)

    assert report.filled == 2
    assert len(calls) == 2
    assert sum(text is None for text in (await _descriptions(db_session)).values()) == 2


async def test_each_filled_posting_gets_a_version_as_a_crawl_would_give_it(db_session):
    from packages.core.models_jobs import PostingVersion

    await _crawled(db_session)
    waiting = {eid for eid, text in (await _descriptions(db_session)).items() if text is None}
    rows = (await db_session.scalars(select(Posting))).all()
    ids = {row.id for row in rows if row.external_id in waiting}
    versions = select(PostingVersion.posting_id).where(PostingVersion.posting_id.in_(ids))
    before = len((await db_session.scalars(versions)).all())

    fetcher, _ = _board()
    await backfill(fetcher, _sessions(db_session))

    after = (await db_session.scalars(versions)).all()
    assert len(after) > before
    assert set(after) == ids


async def _waiting(db_session) -> list[str]:
    """Newest first, as the backfill reads them."""
    pending = await find_pending(db_session)
    return [item.listing.external_id for item in pending[HOST]]


def _number(external_id: str) -> int:
    return int(external_id.rsplit("-", 1)[-1])


async def test_a_host_that_serves_pages_is_not_stopped_by_a_few_refused_ones(db_session):
    """Live, big tenants served four pages in five; three refusals in a row
    happened by chance and stopped them, J.B. Hunt among them."""
    await _crawled(db_session, total=20)  # five read, fifteen waiting
    order = await _waiting(db_session)
    # Served, then three refused in a row, then served again.
    refused = frozenset(_number(eid) for eid in order[1:4])

    fetcher, calls = _board(total=20, refused=refused)
    report = await backfill(fetcher, _sessions(db_session))

    assert report.refused_hosts == []
    assert report.filled == 12
    assert len(calls) == 15


async def test_a_host_that_served_pages_and_then_refuses_ten_in_a_row_is_left_alone(db_session):
    await _crawled(db_session, total=20)
    order = await _waiting(db_session)
    refused = frozenset(_number(eid) for eid in order[1:])

    fetcher, calls = _board(total=20, refused=refused)
    report = await backfill(fetcher, _sessions(db_session))

    assert report.refused_hosts == [HOST]
    assert report.filled == 1
    assert len(calls) == 1 + REFUSALS_BEFORE_STOP_SERVING


async def test_pages_left_waiting_last_time_are_read_after_the_rest(db_session):
    """A restart used to open each host with the pages it had just refused,
    three in a row, and stopped sixteen hosts that were serving pages."""
    await _crawled(db_session, total=20)
    order = await _waiting(db_session)
    tried = frozenset(order[:3])

    fetcher, calls = _board(total=20, refused=frozenset(_number(eid) for eid in tried))
    report = await backfill(fetcher, _sessions(db_session), tried_before=tried)

    assert report.refused_hosts == []
    assert report.filled == 12
    asked = [path.rsplit("/", 1)[-1] for _, path, _ in calls]
    assert {_number(page) for page in asked[-3:]} == {_number(eid) for eid in tried}


async def test_each_page_left_waiting_is_reported_so_a_rerun_can_put_it_last(db_session):
    await _crawled(db_session, total=12)
    order = await _waiting(db_session)
    gone = frozenset({_number(order[0])})

    left: list[str] = []
    fetcher, _ = _board(total=12, gone=gone)
    await backfill(fetcher, _sessions(db_session), on_left_waiting=left.append)

    assert left == [order[0]]


async def test_pages_that_failed_before_can_be_left_as_not_available(db_session):
    """The owner's call on 2026-11-02: a page that failed is left without text,
    not asked for again. Nothing is written in its place: an unknown stays NULL."""
    await _crawled(db_session, total=12)
    order = await _waiting(db_session)
    failed = frozenset(order[:2])

    fetcher, calls = _board(total=12)
    report = await backfill(fetcher, _sessions(db_session), tried_before=failed, retry_tried=False)

    assert report.filled == 5
    assert len(calls) == 5
    texts = await _descriptions(db_session)
    assert all(texts[eid] is None for eid in failed)
