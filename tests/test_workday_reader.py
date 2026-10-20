"""Real public payload and the bounded reader; every request is replayed offline."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from packages.ats.base import UnsupportedSiteError
from packages.ats.registry import adapter_for, detect_ats
from packages.core.models import Company, Posting
from packages.core.models_jobs import PostingVersion
from packages.crawler import workday
from packages.crawler.crawl import crawl_company
from packages.crawler.extract import CompanySeed
from packages.crawler.fetch import PoliteFetcher
from packages.crawler.find_boards import Resolved, board_root, from_url, resolve_site
from packages.crawler.job_systems import other_job_system
from packages.crawler.ratelimit import HostRateLimiter
from tests.test_workday import HOST, SLUG, FakeClock, _job

FIXTURES = Path(__file__).parent / "fixtures" / "workday"


@pytest.mark.parametrize("locale", ["", "en-US/", "en-us/", "es/"])
def test_workday_board_identity_ignores_locale_and_deep_links(locale):
    url = f"https://{HOST}/{locale}AcmeCareers/job/X_R1/apply?source=careers"
    assert board_root(url) == ("workday", SLUG)


@pytest.mark.parametrize(
    "url",
    [
        "https://bad.myworkdayjobs.com/External",
        "https://acme.wd5.myworkdayjobs.com.evil.org/External",
        "https://acme.wd5.myworkdayjobs.com/External%2f..",
        "https://acme.wd5.myworkdayjobs.com/External&quot;garbage",
        "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs",
        "ftp://acme.wd5.myworkdayjobs.com/External",
        "https://user:pass@acme.wd5.myworkdayjobs.com/External",
    ],
)
def test_malformed_identity_is_refused(url):
    assert workday.parse_site(url) is None


def test_recorded_detail_has_description_exact_date_and_stable_identity():
    listing = json.loads((FIXTURES / "ameren-list.json").read_text())
    postings, total = workday.parse_page(
        json.dumps(listing), "ameren.wd1.myworkdayjobs.com", "External"
    )
    assert total == 83
    assert len(postings) == 3
    result = workday.read_detail((FIXTURES / "ameren-detail.json").read_text(), postings[0])
    assert result.external_id == "032867"
    assert "Bachelor of Science Degree in Electrical Engineering" in result.description_raw
    assert "<p" not in result.description_raw
    assert result.published_at == datetime(2026, 10, 8, tzinfo=UTC)
    assert result.content_hash != postings[0].content_hash


def test_path_id_is_stable_when_bullet_fields_are_not_requisition_ids():
    entry = _job(1) | {"bulletFields": ["Full time", "Decatur, IL"]}
    postings, _ = workday.parse_page(
        json.dumps({"total": 1, "jobPostings": [entry]}), HOST, "AcmeCareers"
    )
    assert postings[0].external_id == "JR-00001"


def test_time_type_is_not_used_as_a_requisition_id():
    entry = _job(1) | {"externalPath": "/job/Decatur/Engineer", "bulletFields": ["Full time"]}
    postings, _ = workday.parse_page(
        json.dumps({"total": 1, "jobPostings": [entry]}), HOST, "AcmeCareers"
    )
    assert postings == []


def test_detail_from_a_different_requisition_is_refused():
    postings, _ = workday.parse_page(
        json.dumps({"total": 1, "jobPostings": [_job(1)]}), HOST, "AcmeCareers"
    )
    body = json.dumps({"jobPostingInfo": {"jobDescription": "Other job", "jobReqId": "JR-99999"}})
    with pytest.raises(workday.WorkdayBoardError, match="requisition"):
        workday.read_detail(body, postings[0])


def _reader(
    total=9, *, description="Python systems", status=200, robots="User-agent: *\nDisallow:"
):
    calls = []
    clock = FakeClock()

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        calls.append((request.method, request.url.path, clock.now))
        if status != 200:
            return httpx.Response(status)
        if request.method == "POST":
            offset = json.loads(request.content)["offset"]
            return httpx.Response(
                200,
                json={
                    "total": total,
                    "jobPostings": [_job(n) for n in range(offset, min(total, offset + 20))],
                },
            )
        return httpx.Response(
            200,
            json={
                "jobPostingInfo": {
                    "jobDescription": f"<p>{description}</p>",
                    "startDate": datetime.now(UTC).date().isoformat(),
                }
            },
        )

    fetcher = PoliteFetcher(
        transport=httpx.MockTransport(handler),
        rate_limiter=HostRateLimiter(clock=clock, sleeper=clock.sleep),
    )
    return fetcher, calls


async def test_discovery_counts_the_whole_board_with_one_post():
    fetcher, calls = _reader(total=45)
    result, _ = await resolve_site("Acme", fetcher, url=f"https://{HOST}/AcmeCareers", guess=False)
    assert isinstance(result, Resolved)
    assert result.open_jobs == 45
    assert result.ats == "workday"
    assert result.board_url == f"https://{HOST}/AcmeCareers"
    assert [method for method, _, _ in calls] == ["POST"]


async def test_zero_workday_jobs_is_not_verified():
    fetcher, calls = _reader(total=0)
    result, _ = await resolve_site("Acme", fetcher, url=f"https://{HOST}/AcmeCareers", guess=False)
    assert not isinstance(result, Resolved)
    assert "no open roles" in result[1]
    assert len(calls) == 1


async def test_supported_workday_link_is_not_another_job_system():
    html = f'<a href="https://{HOST}/AcmeCareers">Jobs</a>'
    assert other_job_system(html) is None
    fetcher, calls = _reader()
    reading = await from_url(f"https://{HOST}/AcmeCareers", fetcher)
    assert reading.board == ("workday", SLUG)
    assert calls == []


@pytest.mark.parametrize(
    "status,robots,category",
    [
        (403, "User-agent: *\nDisallow:", "unreachable"),
        (200, "User-agent: *\nDisallow: /wday/cxs/", "blocked"),
    ],
)
async def test_board_refusal_is_reported_and_never_verified(status, robots, category):
    fetcher, calls = _reader(status=status, robots=robots)
    result, reading = await resolve_site(
        "Acme", fetcher, url=f"https://{HOST}/AcmeCareers", guess=False
    )
    assert not isinstance(result, Resolved)
    assert reading.category == category
    if category == "blocked":
        assert calls == []


async def test_bounded_details_make_progress_on_an_unchanged_listing(db_session):
    seed = CompanySeed(name="Acme", slug=SLUG, ats="workday", poll_interval_s=0)
    fetcher, calls = _reader()
    first = await crawl_company(db_session, seed, fetcher, force=True)
    assert first.new_postings == 9
    assert len([c for c in calls if c[0] == "GET"]) == 5
    rows = (await db_session.scalars(select(Posting))).all()
    assert sum(bool(row.description_raw) for row in rows) == 5
    second, _ = _reader()
    result = await crawl_company(db_session, seed, second, force=True)
    assert result.updated_postings == 4
    db_session.expire_all()
    rows = (await db_session.scalars(select(Posting))).all()
    assert all(row.description_raw == "Python systems" for row in rows)
    third, _ = _reader()
    result = await crawl_company(db_session, seed, third, force=True)
    assert result.unchanged
    assert result.emitted == 0
    assert all(b[2] - a[2] >= 60 for a, b in zip(calls, calls[1:], strict=False))


async def test_description_only_edit_is_versioned_on_identical_list(db_session):
    seed = CompanySeed(name="Acme", slug=SLUG, ats="workday", poll_interval_s=0)
    fetcher, _ = _reader(total=1)
    await crawl_company(db_session, seed, fetcher, force=True)
    fetcher, _ = _reader(total=1, description="Rust services")
    result = await crawl_company(db_session, seed, fetcher, force=True)
    assert result.updated_postings == 1
    row = await db_session.scalar(select(Posting).execution_options(populate_existing=True))
    assert row.description_raw == "Rust services"
    versions = (await db_session.scalars(select(PostingVersion))).all()
    assert len(versions) == 2


async def test_posting_age_lower_bound_is_not_an_invented_date(db_session, monkeypatch):
    from packages.core.config import get_settings

    monkeypatch.setattr(get_settings(), "posting_max_age_days", 7)
    fetcher, calls = _reader(total=1)
    result = await crawl_company(
        db_session,
        CompanySeed(name="Acme", slug=SLUG, ats="workday", poll_interval_s=0),
        fetcher,
        force=True,
    )
    assert result.new_postings == 1  # Posted 3 Days Ago, then exact date in detail.
    old, _ = workday.parse_page(
        json.dumps({"total": 1, "jobPostings": [_job(2) | {"postedOn": "Posted 30+ Days Ago"}]}),
        HOST,
        "AcmeCareers",
    )
    from packages.crawler.crawl import _store

    company = await db_session.scalar(select(Company))
    stored = await _store(db_session, company, old, max_age_days=7)
    assert stored.too_old == 1
    assert old[0].published_at is None


def test_workday_apply_stays_unsupported():
    url = f"https://{HOST}/AcmeCareers/job/X_R1/apply"
    assert detect_ats(url) is None
    with pytest.raises(UnsupportedSiteError):
        adapter_for(url)


async def test_read_only_probe_fetcher_does_not_write_metrics(monkeypatch):
    from unittest.mock import AsyncMock

    metric = AsyncMock()
    monkeypatch.setattr("packages.crawler.fetch.record_request", metric)
    fetcher = PoliteFetcher(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="User-agent: *\nDisallow:")
        ),
        record_metrics=False,
    )
    async with fetcher:
        await fetcher.fetch("https://acme.example/jobs")
    metric.assert_not_awaited()
