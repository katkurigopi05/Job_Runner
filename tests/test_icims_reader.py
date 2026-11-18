"""iCIMS/Jibe public careers API, recorded payloads and polite offline replay."""

import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from packages.core.models import Posting
from packages.crawler import icims
from packages.crawler.crawl import crawl_company
from packages.crawler.extract import CompanySeed, extractor_for
from packages.crawler.fetch import Blocked, PoliteFetcher
from packages.crawler.find_boards import _read_page, resolve_site
from packages.crawler.ratelimit import HostRateLimiter
from tests.test_workday import FakeClock

FIX = Path(__file__).parent / "fixtures" / "icims"
LIST = (FIX / "aon-list.json").read_text()
PAGE = (FIX / "aon-page.html").read_text()
ORIGIN = "https://jobs.aon.com"


def _fetcher(payload=LIST, *, robots="User-agent: *\nDisallow: /login", status=200):
    calls = []
    clock = FakeClock()

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        calls.append((request.url.path, clock.now))
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, text=payload if request.url.path == "/api/jobs" else PAGE)

    return PoliteFetcher(
        transport=httpx.MockTransport(handler),
        rate_limiter=HostRateLimiter(clock=clock, sleeper=clock.sleep),
    ), calls


def test_real_list_reads_text_date_and_country_without_an_application_request():
    jobs, total = icims.parse_page(LIST, ORIGIN)
    assert (len(jobs), total) == (2, 1073)
    job = jobs[0]
    assert job.external_id == "112347"
    assert job.url == ORIGIN + "/jobs/112347"
    assert "/login" not in job.url
    assert job.source_locations[0]["country_code"] == "US"
    assert "Charlotte" in job.location and job.description_raw
    assert job.published_at is not None
    assert extractor_for("icims") is not None


def test_real_additional_locations_are_all_kept():
    job = icims.parse_page((FIX / "aon-multiple.json").read_text(), ORIGIN)[0][0]
    assert len(job.source_locations) == 7
    assert "Houston" in job.location and "New York" in job.location


def test_unknown_country_is_not_inferred_from_locale_or_company():
    d = json.loads(LIST)
    v = d["jobs"][0]["data"]
    for key in ("city", "state", "country", "country_code", "location_name"):
        v.pop(key, None)
    job = icims.parse_page(json.dumps(d), ORIGIN)[0][0]
    assert job.location is None and job.source_locations == []


@pytest.mark.parametrize(
    "body",
    [
        "{}",
        '{"jobs":[]}',
        "<html>Sign in</html>",
        '{"jobs":[],"totalCount":-1}',
        '{"jobs":[{}],"totalCount":1}',
    ],
)
def test_malformed_payload_never_means_empty_board(body):
    with pytest.raises(icims.IcimsBoardError):
        icims.parse_page(body, ORIGIN)


def test_only_page_configuration_plus_vendor_script_names_custom_host():
    assert icims.from_page(PAGE, ORIGIN + "/jobs") == ORIGIN
    assert icims.from_page("<p>We use iCIMS</p>", ORIGIN) is None
    assert icims.from_page('<a href="https://www.icims.com/privacy">privacy</a>', ORIGIN) is None
    assert (
        icims.from_page(PAGE.replace("app.jibecdn.com", "app.jibecdn.com.evil.example"), ORIGIN)
        is None
    )
    assert (
        _read_page(
            PAGE + '<a href="https://boards.greenhouse.io/another">Other company</a>', ORIGIN
        ).board
        is None
    )


@pytest.mark.parametrize(
    "slug",
    [
        "http://jobs.aon.com",
        "https://user:pass@jobs.aon.com",
        "https://jobs.aon.com/api/jobs",
        "https://jobs.aon.com?x=1",
        "https://jobs.aon.com:444",
    ],
)
def test_invalid_board_identity_is_refused(slug):
    with pytest.raises(ValueError):
        icims.IcimsExtractor().board_url(slug)


async def test_discovery_reads_site_then_one_api_page():
    f, calls = _fetcher()
    async with f:
        result, _ = await resolve_site("Aon", f, url=ORIGIN, guess=False)
    assert result.ats == "icims" and result.slug == ORIGIN and result.open_jobs == 1073
    assert [p for p, _ in calls] == ["/", "/api/jobs"]
    assert calls[1][1] - calls[0][1] >= 5


async def test_crawl_persists_through_store_and_normalizes_locations(db_session):
    d = json.loads(LIST)
    d["totalCount"] = 2
    f, calls = _fetcher(json.dumps(d))
    async with f:
        result = await crawl_company(
            db_session, CompanySeed(name="Aon", ats="icims", slug=ORIGIN), f, force=True
        )
    assert result.new_postings == 2 and not result.error
    rows = (await db_session.scalars(select(Posting))).all()
    assert all(r.country_code == "US" and r.locations_json for r in rows)
    assert all(r.description_raw for r in rows)
    assert len(calls) == 1


async def test_robots_refusal_stops_before_api():
    f, calls = _fetcher(robots="User-agent: *\nDisallow: /api/")
    async with f:
        with pytest.raises(Blocked):
            await icims.IcimsExtractor().collect(f, ORIGIN)
    assert calls == []


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_refusal_stops_without_retries(status):
    f, calls = _fetcher(status=status)
    async with f:
        with pytest.raises(icims.IcimsBoardError):
            await icims.IcimsExtractor().collect(f, ORIGIN)
    assert len(calls) == 1


async def test_short_or_repeated_page_is_truncated(monkeypatch):
    f, _ = _fetcher()
    async with f:
        assert (await icims.IcimsExtractor().collect(f, ORIGIN)).truncated
    monkeypatch.setattr(icims, "PAGE_SIZE", 2)
    monkeypatch.setattr(icims, "MAX_PAGES", 2)
    f, _ = _fetcher()
    async with f:
        pages = await icims.IcimsExtractor().collect(f, ORIGIN)
    assert pages.truncated and len(pages.postings) == 2


async def test_distinct_pages_complete_and_honor_host_delay(monkeypatch):
    monkeypatch.setattr(icims, "PAGE_SIZE", 1)
    payload = json.loads(LIST)
    clock = FakeClock()
    calls = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /login")
        page = int(request.url.params["page"])
        calls.append(clock.now)
        return httpx.Response(200, json={"totalCount": 2, "jobs": [payload["jobs"][page - 1]]})

    async with PoliteFetcher(
        transport=httpx.MockTransport(handler),
        rate_limiter=HostRateLimiter(clock=clock, sleeper=clock.sleep),
    ) as f:
        pages = await icims.IcimsExtractor().collect(f, ORIGIN)
    assert not pages.truncated and len(pages.postings) == 2
    assert calls[1] - calls[0] >= 5


async def test_page_limit_cannot_claim_completeness(monkeypatch):
    monkeypatch.setattr(icims, "PAGE_SIZE", 2)
    monkeypatch.setattr(icims, "MAX_PAGES", 1)
    f, calls = _fetcher()
    async with f:
        pages = await icims.IcimsExtractor().collect(f, ORIGIN)
    assert pages.truncated and len(calls) == 1


def test_empty_public_board_is_valid_but_internal_jobs_are_refused():
    assert icims.parse_page('{"jobs":[],"totalCount":0}', ORIGIN) == ([], 0)
    d = json.loads(LIST)
    d["jobs"][0]["data"]["internal"] = True
    with pytest.raises(icims.IcimsBoardError):
        icims.parse_page(json.dumps(d), ORIGIN)


@pytest.fixture(autouse=True)
def _recorded_fixtures_do_not_age_out(monkeypatch):
    from packages.core.config import get_settings

    monkeypatch.setattr(get_settings(), "posting_max_age_days", 0)


def test_reading_does_not_enable_applying():
    from packages.ats.base import UnsupportedSiteError
    from packages.ats.registry import adapter_for

    with pytest.raises(UnsupportedSiteError):
        adapter_for("https://jobs.aon.com/jobs")


async def test_truncated_crawl_keeps_an_unlisted_held_job_open(db_session):
    from datetime import UTC, datetime, timedelta

    from packages.core.models import Company
    from packages.crawler.crawl import _store
    from tests.test_crawl_skips_abroad import _posting

    company = Company(name="Aon", ats_type="icims", slug=ORIGIN)
    db_session.add(company)
    await db_session.flush()
    await _store(
        db_session,
        company,
        [_posting("held", "San Francisco, CA")],
        now=datetime.now(UTC) - timedelta(days=1),
        max_age_days=0,
    )
    f, _ = _fetcher()
    async with f:
        result = await crawl_company(
            db_session, CompanySeed(name="Aon", ats="icims", slug=ORIGIN), f, force=True
        )
    held = await db_session.scalar(select(Posting).where(Posting.external_id == "held"))
    assert result.suspect_parse and result.closed_postings == 0
    assert held.closed_at is None
