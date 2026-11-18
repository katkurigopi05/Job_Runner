"""Public Oracle Candidate Experience: recorded payloads replayed offline."""

import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from packages.core.models import Posting
from packages.crawler import oracle
from packages.crawler.crawl import crawl_company
from packages.crawler.extract import CompanySeed, extractor_for
from packages.crawler.fetch import Blocked, PoliteFetcher
from packages.crawler.find_boards import board_root, resolve_site
from packages.crawler.ratelimit import HostRateLimiter
from packages.crawler.validate import _api_is_board
from tests.test_board_shape import _response
from tests.test_workday import FakeClock

FIX = Path(__file__).parent / "fixtures" / "oracle"
LIST = (FIX / "camden-list.json").read_text()
DETAIL = (FIX / "camden-detail.json").read_text()
SITE = "https://ehap.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX"
SLUG = "ehap.fa.us2.oraclecloud.com|CX"


def _fetcher(payload=LIST, *, robots="User-agent: *\nDisallow: /login", status=200):
    clock = FakeClock()
    calls = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        calls.append((str(request.url), clock.now))
        if status != 200:
            return httpx.Response(status)
        if "RequisitionDetails" in request.url.path:
            return httpx.Response(200, text=DETAIL)
        return httpx.Response(200, text=payload)

    return PoliteFetcher(
        transport=httpx.MockTransport(handler),
        rate_limiter=HostRateLimiter(clock=clock, sleeper=clock.sleep),
    ), calls


def _one():
    d = json.loads(LIST)
    d["items"][0]["requisitionList"] = d["items"][0]["requisitionList"][:1]
    d["items"][0]["TotalJobsCount"] = 1
    return json.dumps(d)


def test_recorded_list_and_detail_preserve_source_facts():
    jobs, total, offset = oracle.parse_page(LIST, SLUG)
    assert (len(jobs), total, offset) == (2, 54, 0)
    assert jobs[0].external_id == "260670"
    assert jobs[0].url == SITE + "/job/260670"
    assert jobs[0].source_locations[0]["country_code"] == "US"
    assert jobs[0].description_raw is None
    full = oracle.read_detail(DETAIL, jobs[0])
    assert "Maintenance Technician" in full.description_raw
    assert "<p>" not in full.description_raw
    assert full.content_hash != jobs[0].content_hash
    assert full.published_at is not None


@pytest.mark.parametrize(
    "url,expected",
    [
        (SITE, SLUG),
        (SITE + "/job/260670", SLUG),
        ("https://ehap.fa.us2.oraclecloud.com:443/hcmUI/CandidateExperience/en/sites/CX", SLUG),
        ("https://ehap.fa.us2.oraclecloud.com", None),
        (SITE.replace("oraclecloud.com", "oraclecloud.com.evil.example"), None),
        (SITE.replace("https://", "https://user:pass@"), None),
        (SITE.replace("/CX", "/CX%2Fbad"), None),
        ("https://careers.example.com/login", None),
    ],
)
def test_only_explicit_oracle_sites_name_a_board(url, expected):
    assert oracle.slug_for(url) == expected
    assert board_root(url) == (("oracle", expected) if expected else None)


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        '{"items":[]}',
        "<html>Sign in</html>",
        '{"items":[{"TotalJobsCount":-1,"Offset":0,"requisitionList":[]}]}',
    ],
)
def test_malformed_or_login_payload_is_not_an_empty_board(payload):
    with pytest.raises(oracle.OracleBoardError):
        oracle.parse_page(payload, SLUG)
    assert not _api_is_board(_response(payload), "oracle")


def test_detail_must_belong_to_the_listing():
    listing = oracle.parse_page(LIST, SLUG)[0][1]
    with pytest.raises(oracle.OracleBoardError):
        oracle.read_detail(DETAIL, listing)


def test_secondary_office_and_unknown_country_are_not_invented():
    d = json.loads(LIST)
    entry = d["items"][0]["requisitionList"][0]
    entry.update(
        PrimaryLocation="Office 1",
        PrimaryLocationCountry=None,
        secondaryLocations=[{"Name": "San Francisco, CA"}],
    )
    item = oracle.parse_page(json.dumps(d), SLUG)[0][0]
    assert len(item.source_locations) == 2
    assert item.source_locations[0]["country_code"] is None
    assert item.location == "Office 1; San Francisco, CA"


async def test_discovery_reads_one_page_and_reports_total():
    f, calls = _fetcher()
    async with f:
        result, _ = await resolve_site("Camden", f, url=SITE, guess=False)
    assert result.ats == "oracle" and result.open_jobs == 54
    assert len(calls) == 1
    assert extractor_for("oracle") is not None


async def test_crawl_uses_normal_storage_and_bounded_details(db_session, monkeypatch):
    monkeypatch.setattr("packages.crawler.details.DETAIL_REQUESTS_PER_CRAWL", 1)
    f, calls = _fetcher(_one())
    async with f:
        result = await crawl_company(
            db_session, CompanySeed(name="Camden", ats="oracle", slug=SLUG), f, force=True
        )
    assert result.new_postings == 1 and not result.error
    row = await db_session.scalar(select(Posting))
    assert row.country_code == "US" and row.state_code == "AZ"
    assert row.description_raw and row.locations_json
    assert calls[1][1] - calls[0][1] >= 5


async def test_robots_refusal_never_reads_the_api():
    f, calls = _fetcher(robots="User-agent: *\nDisallow: /hcmRestApi/")
    async with f:
        with pytest.raises(Blocked):
            await oracle.OracleExtractor().collect(f, SLUG)
    assert calls == []


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_refusal_stops_board_without_retry(status):
    f, calls = _fetcher(status=status)
    async with f:
        with pytest.raises(oracle.OracleBoardError):
            await oracle.OracleExtractor().collect(f, SLUG)
    assert len(calls) == 1


async def test_incomplete_page_marks_board_truncated():
    f, _ = _fetcher()
    async with f:
        pages = await oracle.OracleExtractor().collect(f, SLUG)
    assert pages.truncated


async def test_pagination_and_duplicate_page_never_claim_completeness(monkeypatch):
    monkeypatch.setattr(oracle, "PAGE_SIZE", 2)
    monkeypatch.setattr(oracle, "MAX_PAGES", 2)
    d = json.loads(LIST)
    d["items"][0]["TotalJobsCount"] = 4
    f, _ = _fetcher(json.dumps(d))
    async with f:
        pages = await oracle.OracleExtractor().collect(f, SLUG)
    assert pages.truncated
    assert len({p.external_id for p in pages.postings}) == len(pages.postings)


async def test_distinct_pages_complete_in_order_with_host_delay(monkeypatch):
    monkeypatch.setattr(oracle, "PAGE_SIZE", 2)
    first = json.loads(LIST)
    second = json.loads((FIX / "camden-page2.json").read_text())
    for data, offset in [(first, 0), (second, 2)]:
        data["items"][0].update(TotalJobsCount=4, Offset=offset, Limit=2)
    clock = FakeClock()
    calls = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /login")
        calls.append(clock.now)
        return httpx.Response(
            200, json=first if "offset=0" in request.url.params["finder"] else second
        )

    async with PoliteFetcher(
        transport=httpx.MockTransport(handler),
        rate_limiter=HostRateLimiter(clock=clock, sleeper=clock.sleep),
    ) as f:
        pages = await oracle.OracleExtractor().collect(f, SLUG)
    assert not pages.truncated and len(pages.postings) == 4
    assert calls[1] - calls[0] >= 5


def test_empty_site_and_wrong_site_are_distinct():
    d = json.loads(LIST)
    d["items"][0].update(TotalJobsCount=0, requisitionList=[])
    assert oracle.parse_page(json.dumps(d), SLUG) == ([], 0, 0)
    d["items"][0]["SiteNumber"] = "Internal"
    with pytest.raises(oracle.OracleBoardError):
        oracle.parse_page(json.dumps(d), SLUG)


async def test_detail_refusal_prevents_partial_storage(db_session, monkeypatch):
    clock = FakeClock()

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /login")
        if "RequisitionDetails" in request.url.path:
            return httpx.Response(403)
        return httpx.Response(200, text=_one())

    async with PoliteFetcher(
        transport=httpx.MockTransport(handler),
        rate_limiter=HostRateLimiter(clock=clock, sleeper=clock.sleep),
    ) as f:
        result = await crawl_company(
            db_session, CompanySeed(name="Camden", ats="oracle", slug=SLUG), f, force=True
        )
    assert result.error
    assert (await db_session.scalars(select(Posting))).all() == []


@pytest.fixture(autouse=True)
def _recorded_fixtures_do_not_age_out(monkeypatch):
    from packages.core.config import get_settings

    monkeypatch.setattr(get_settings(), "posting_max_age_days", 0)


def test_reading_does_not_enable_applying():
    from packages.ats.base import UnsupportedSiteError
    from packages.ats.registry import adapter_for

    with pytest.raises(UnsupportedSiteError):
        adapter_for("https://ehap.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX")


async def test_truncated_crawl_keeps_an_unlisted_held_job_open(db_session, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from packages.core.models import Company
    from packages.crawler.crawl import _store
    from tests.test_crawl_skips_abroad import _posting

    monkeypatch.setattr("packages.crawler.details.DETAIL_REQUESTS_PER_CRAWL", 0)
    company = Company(name="Camden", ats_type="oracle", slug=SLUG)
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
            db_session, CompanySeed(name="Camden", ats="oracle", slug=SLUG), f, force=True
        )
    held = await db_session.scalar(select(Posting).where(Posting.external_id == "held"))
    assert result.suspect_parse and result.closed_postings == 0
    assert held.closed_at is None


def test_company_page_links_discover_the_explicit_oracle_site():
    from packages.crawler.find_boards import _read_page
    from packages.crawler.job_systems import other_job_system

    html = f'<a href="{SITE}">Open jobs</a>'
    assert _read_page(html, "https://www.camdenliving.com/careers").board == ("oracle", SLUG)
    assert other_job_system(html) is None
