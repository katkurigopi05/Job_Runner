"""Rippling boards: recorded public pages, every request replayed offline."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from packages.core.models import Posting
from packages.crawler import rippling
from packages.crawler.crawl import crawl_company
from packages.crawler.extract import CompanySeed, extractor_for
from packages.crawler.fetch import PoliteFetcher
from packages.crawler.find_boards import Resolved, board_root, resolve_site
from packages.crawler.job_systems import other_job_system
from packages.crawler.ratelimit import HostRateLimiter
from packages.crawler.validate import _api_is_board
from tests.test_board_shape import _response
from tests.test_workday import FakeClock

FIXTURES = Path(__file__).parent / "fixtures" / "rippling"
PAGE_0 = (FIXTURES / "branch-page-0.html").read_text()
PAGE_1 = (FIXTURES / "branch-page-1.html").read_text()
DETAIL = (FIXTURES / "branch-detail.html").read_text()
FIRST = "454f3d28-ca85-4640-9cfe-c48f1931cfdc"


def _board(*, robots: str = "User-agent: *\nDisallow: /internal/", status: int = 200):
    calls: list[tuple[str, float]] = []
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        target = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        calls.append((target, clock.now))
        if status != 200:
            return httpx.Response(status)
        if target == "/branch/jobs":
            return httpx.Response(200, text=PAGE_0)
        if target == "/branch/jobs?page=1":
            return httpx.Response(200, text=PAGE_1)
        if request.url.path == f"/branch/jobs/{FIRST}":
            return httpx.Response(200, text=DETAIL)
        return httpx.Response(404)

    fetcher = PoliteFetcher(
        transport=httpx.MockTransport(handler),
        rate_limiter=HostRateLimiter(clock=clock, sleeper=clock.sleep),
    )
    return fetcher, calls


def test_a_page_names_its_postings_and_the_board_totals():
    postings, pages, items, entries = rippling.parse_page(PAGE_0, "branch")
    assert (pages, items, entries) == (2, 4, 2)
    first, remote = postings
    assert first.external_id == FIRST
    assert first.url == f"https://ats.rippling.com/branch/jobs/{FIRST}"
    assert first.location == "London, United Kingdom"
    assert remote.location == "Remote (Vancouver, British Columbia, CA)"
    assert first.description_raw is None and first.published_at is None


def test_the_job_page_gives_role_then_company_text_and_its_date():
    listing = rippling.parse_page(PAGE_0, "branch")[0][0]
    result = rippling.read_detail(DETAIL, listing)
    assert result.description_raw and "<p" not in result.description_raw
    assert len(result.description_raw) > 1000
    assert result.published_at is not None
    assert result.published_at.utcoffset() == timedelta(hours=-7)
    assert result.published_at.date() == datetime(2026, 10, 12).date()


def test_a_job_page_for_another_posting_is_refused():
    other = rippling.parse_page(PAGE_0, "branch")[0][1]
    with pytest.raises(rippling.RipplingBoardError):
        rippling.read_detail(DETAIL, other)


@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://ats.rippling.com/branch/jobs", "branch"),
        ("https://ats.rippling.com/branch", "branch"),
        (f"https://ats.rippling.com/branch/jobs/{FIRST}", "branch"),
        ("https://ats.rippling.com/earnup-inc/jobs?page=1", "earnup-inc"),
        ("https://ats.rippling.com/internal/x", None),
        ("https://ats.rippling.com/branch/settings", None),
        ("https://ats.rippling.com.evil.net/branch/jobs", None),
        ("https://www.rippling.com/careers", None),
    ],
)
def test_a_board_is_named_by_its_board_or_job_links(url, slug):
    assert rippling.slug_for(url) == slug
    assert board_root(url) == (("rippling", slug) if slug else None)


def test_a_rippling_link_is_a_board_not_another_job_system():
    assert other_job_system('<a href="https://ats.rippling.com/branch/jobs">Jobs</a>') is None


def test_a_board_page_has_a_board_shape():
    assert rippling.is_board_page(PAGE_0)
    assert _api_is_board(_response(PAGE_0), "rippling")
    assert not _api_is_board(_response("<html><body>Not found</body></html>"), "rippling")


async def test_collect_reads_every_page_and_marks_nothing_truncated():
    fetcher, calls = _board()
    pages = await rippling.RipplingExtractor().collect(fetcher, "branch")
    assert [c for c, _ in calls] == ["/branch/jobs", "/branch/jobs?page=1"]
    assert len(pages.postings) == 3 and not pages.truncated


async def test_a_job_listed_once_per_city_is_one_posting_with_every_place():
    fetcher, _ = _board()
    pages = await rippling.RipplingExtractor().collect(fetcher, "branch")
    [first] = [p for p in pages.postings if p.external_id == FIRST]
    assert first.location == "London, United Kingdom; Bengaluru, India"
    assert len({p.external_id for p in pages.postings}) == len(pages.postings)


async def test_a_board_that_changes_mid_read_closes_nothing(monkeypatch):
    fetcher, _ = _board()
    changed = PAGE_1.replace('"totalItems": 4', '"totalItems": 5')
    original = rippling.parse_page

    def parse(body, slug):
        return original(changed if body == PAGE_1 else body, slug)

    monkeypatch.setattr(rippling, "parse_page", parse)
    pages = await rippling.RipplingExtractor().collect(fetcher, "branch")
    assert pages.truncated


async def test_discovery_counts_the_whole_board_from_its_first_page():
    fetcher, calls = _board()
    result, _ = await resolve_site(
        "Branch", fetcher, url="https://ats.rippling.com/branch/jobs", guess=False
    )
    assert isinstance(result, Resolved)
    # Two pages, so the probe reports Rippling's row total: one request, not a full read.
    assert (result.ats, result.slug, result.open_jobs) == ("rippling", "branch", 4)
    assert len(calls) == 1


async def test_a_crawl_stores_every_page_and_reads_job_pages_within_budget(db_session, monkeypatch):
    from packages.core.config import get_settings

    # One of Branch's jobs is abroad, which a US-only search does not store
    # (tests/test_crawl_skips_abroad.py). This test is about paging and the
    # job-page budget, so it reads the whole board.
    monkeypatch.setattr(get_settings(), "search_us_only", False)
    seed = CompanySeed(name="Branch", slug="branch", ats="rippling", poll_interval_s=0)
    fetcher, calls = _board()
    result = await crawl_company(db_session, seed, fetcher, force=True)

    assert result.new_postings == 3 and not result.error
    assert len(calls) == 2 + 3
    assert all(b[1] - a[1] >= 60 for a, b in zip(calls, calls[1:], strict=False))
    rows = {row.external_id: row for row in (await db_session.scalars(select(Posting))).all()}
    assert rows[FIRST].description_raw and rows[FIRST].published_at is not None
    assert sum(row.description_raw is None for row in rows.values()) == 2


def test_the_reader_is_found_by_name():
    assert isinstance(extractor_for("rippling"), rippling.RipplingExtractor)
