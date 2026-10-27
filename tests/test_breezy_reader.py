"""Breezy boards: a recorded public list and job page, every request replayed offline."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from packages.core.models import Posting
from packages.crawler import breezy
from packages.crawler.crawl import crawl_company
from packages.crawler.extract import CompanySeed, extractor_for
from packages.crawler.fetch import PoliteFetcher
from packages.crawler.find_boards import Resolved, board_root, resolve_site
from packages.crawler.job_systems import other_job_system
from packages.crawler.ratelimit import HostRateLimiter
from packages.crawler.validate import _api_is_board
from tests.test_board_shape import _response
from tests.test_workday import FakeClock

FIXTURES = Path(__file__).parent / "fixtures" / "breezy"
LIST = (FIXTURES / "gozem-list.json").read_text()
DETAIL = (FIXTURES / "gozem-detail.html").read_text()
FIRST = "122cd38baac5"


def _board(*, robots: str = "User-agent: *\nDisallow: /css", status: int = 200):
    """Serve the recorded list, the recorded page for the first job, 404 for the rest."""
    calls: list[tuple[str, float]] = []
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        calls.append((request.url.path, clock.now))
        if status != 200:
            return httpx.Response(status, text="<title>Career portal not found | Breezy HR</title>")
        if request.url.path == "/json":
            return httpx.Response(200, text=LIST)
        if request.url.path.startswith(f"/p/{FIRST}"):
            return httpx.Response(200, text=DETAIL)
        return httpx.Response(404)

    fetcher = PoliteFetcher(
        transport=httpx.MockTransport(handler),
        rate_limiter=HostRateLimiter(clock=clock, sleeper=clock.sleep),
    )
    return fetcher, calls


def test_the_recorded_list_reads_places_in_words_and_keeps_its_dates():
    postings, entries = breezy.parse_list(LIST, "gozem")
    assert entries == 3 and len(postings) == 3
    first, multi, remote = postings
    assert first.external_id == FIRST
    assert first.url == "https://gozem.breezy.hr/p/122cd38baac5-agent-support-client"
    assert first.location == "Lomé, Togo"
    assert first.published_at == datetime(2026, 10, 26, 15, 26, 57, 725000, tzinfo=UTC)
    assert first.description_raw is None and first.ats_type == "breezy"
    assert multi.location.count(";") >= 1
    assert remote.location == "Remote (United States)"


def test_the_job_page_gives_the_text_and_changes_the_hash():
    listing = breezy.parse_list(LIST, "gozem")[0][0]
    result = breezy.read_detail(DETAIL, listing)
    assert result.description_raw and len(result.description_raw) > 500
    assert "<p" not in result.description_raw
    assert result.published_at == listing.published_at  # the list's exact time wins
    assert result.content_hash != listing.content_hash


def test_a_page_without_a_job_posting_is_refused():
    listing = breezy.parse_list(LIST, "gozem")[0][0]
    with pytest.raises(breezy.BreezyBoardError):
        breezy.read_detail("<html><title>Gone</title></html>", listing)


@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://gozem.breezy.hr/", "gozem"),
        ("https://gozem.breezy.hr/p/122cd38baac5-agent-support-client", "gozem"),
        ("https://Gozem.breezy.hr/json", "gozem"),
        ("https://gozem.breezy.hr.evil.net/", None),
        ("https://www.breezy.hr/", None),
        ("https://breezy.hr/", None),
        ("ftp://gozem.breezy.hr/", None),
    ],
)
def test_a_board_is_named_by_any_page_on_its_subdomain(url, slug):
    assert breezy.slug_for(url) == slug
    assert board_root(url) == (("breezy", slug) if slug else None)


@pytest.mark.parametrize("slug", ["evil.com/x", "-gozem", "a" * 64, "go_zem", ""])
def test_a_slug_that_is_not_a_subdomain_is_refused(slug):
    with pytest.raises(ValueError):
        breezy.BreezyExtractor().board_url(slug)


def test_a_breezy_link_is_a_board_not_another_job_system():
    html = '<a href="https://gozem.breezy.hr/p/122cd38baac5">Apply</a>'
    assert other_job_system(html) is None


def test_the_list_is_a_board_shape_even_when_empty():
    assert _api_is_board(_response(LIST), "breezy")
    assert _api_is_board(_response("[]"), "breezy")
    assert not _api_is_board(_response(json.dumps({"jobs": []})), "breezy")


async def test_discovery_verifies_a_breezy_board_with_one_request():
    fetcher, calls = _board()
    result, _ = await resolve_site("Gozem", fetcher, url="https://gozem.breezy.hr/", guess=False)
    assert isinstance(result, Resolved)
    assert (result.ats, result.slug, result.open_jobs) == ("breezy", "gozem", 3)
    assert [path for path, _ in calls] == ["/json"]


async def test_a_missing_portal_is_never_verified():
    fetcher, _ = _board(status=404)
    result, _ = await resolve_site("Kayhan", fetcher, url="https://kayhan.breezy.hr/", guess=False)
    assert not isinstance(result, Resolved)


async def test_a_crawl_stores_the_list_and_reads_job_pages_within_budget(db_session):
    seed = CompanySeed(name="Gozem", slug="gozem", ats="breezy", poll_interval_s=0)
    fetcher, calls = _board()
    result = await crawl_company(db_session, seed, fetcher, force=True)

    assert result.new_postings == 3 and not result.error
    # One list, then a page for each of the three: within the five-page budget.
    assert [path for path, _ in calls][0] == "/json" and len(calls) == 4
    assert all(b[1] - a[1] >= 60 for a, b in zip(calls, calls[1:], strict=False))
    rows = {row.external_id: row for row in (await db_session.scalars(select(Posting))).all()}
    assert rows[FIRST].description_raw and len(rows[FIRST].description_raw) > 500
    # A job page that failed leaves its posting stored, waiting for its text.
    assert sum(row.description_raw is None for row in rows.values()) == 2


async def test_robots_refusing_job_pages_skips_the_board(db_session):
    seed = CompanySeed(name="Gozem", slug="gozem", ats="breezy", poll_interval_s=0)
    fetcher, _ = _board(robots="User-agent: *\nDisallow: /p/")
    result = await crawl_company(db_session, seed, fetcher, force=True)
    assert result.blocked
    assert (await db_session.scalars(select(Posting))).all() == []


def test_the_reader_is_found_by_name():
    assert isinstance(extractor_for("breezy"), breezy.BreezyExtractor)
