"""Workday: the board that takes more than one request to read.

**The payload shape here is unverified against a live tenant.** This
environment has no egress to `*.myworkdayjobs.com`, so the fixtures below were
written from the API's documented shape rather than recorded from a real
board. CLAUDE.md §15 says exactly what that is worth: the hand-written
Greenhouse fixture had a native `<select>` while the live board had moved to
react-select, the adapter misread every dropdown, and the suite stayed green
the whole time.

So read these as tests of the *pagination and failure handling*, which is
where the risk specific to Workday lives, and not as evidence that the field
names are right. They are the half that can be established offline.
"""

from __future__ import annotations

import json

import httpx
import pytest

from packages.crawler.extract import extractor_for
from packages.crawler.fetch import Blocked, PoliteFetcher
from packages.crawler.ratelimit import HostRateLimiter
from packages.crawler.workday import (
    MAX_PAGES,
    PAGE_SIZE,
    WorkdayBoardError,
    WorkdayExtractor,
    parse_page,
    parse_site,
    slug_for,
)

HOST = "acme.wd5.myworkdayjobs.com"
SLUG = f"{HOST}|acme|AcmeCareers"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _job(n: int) -> dict:
    return {
        "title": f"Software Engineer {n}",
        "externalPath": f"/job/San-Jose/Software-Engineer_JR-{n:05d}",
        "locationsText": "San Jose, CA",
        "postedOn": "Posted 3 Days Ago",
        "bulletFields": [f"JR-{n:05d}"],
    }


def _fetcher(total: int, *, fail_page: int | None = None, robots: str = "User-agent: *\nDisallow:"):
    """A board of `total` postings, served `PAGE_SIZE` at a time."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        body = json.loads(request.content.decode())
        offset = int(body["offset"])
        page = offset // PAGE_SIZE
        calls.append(offset)
        if fail_page is not None and page == fail_page:
            return httpx.Response(500, text="boom")
        jobs = [_job(n) for n in range(offset, min(offset + PAGE_SIZE, total))]
        return httpx.Response(200, text=json.dumps({"total": total, "jobPostings": jobs}))

    clock = FakeClock()
    fetcher = PoliteFetcher(
        transport=httpx.MockTransport(handler),
        rate_limiter=HostRateLimiter(clock=clock, sleeper=clock.sleep),
    )
    return fetcher, calls


# --------------------------------------------------------------------------
# Reading a careers URL
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://acme.wd5.myworkdayjobs.com/en-US/AcmeCareers", (HOST, "acme", "AcmeCareers")),
        ("https://acme.wd5.myworkdayjobs.com/AcmeCareers", (HOST, "acme", "AcmeCareers")),
        (
            "https://acme.wd5.myworkdayjobs.com/AcmeCareers/job/X_JR-1",
            (HOST, "acme", "AcmeCareers"),
        ),
    ],
)
def test_a_careers_url_yields_host_tenant_and_site(url, expected) -> None:
    assert parse_site(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/acme",
        "https://acme.com/careers",
        "not a url at all",
        "https://acme.wd5.myworkdayjobs.com/",
    ],
)
def test_a_non_workday_url_is_refused_rather_than_guessed(url) -> None:
    """Tenant, data-centre number and site name are three independent facts.

    None can be derived from a company name the way a Greenhouse slug often
    can, so there is nothing to guess with — and a wrong tenant points the
    crawler at another employer's board.
    """
    assert parse_site(url) is None
    assert slug_for(url) is None


def test_a_slug_that_is_not_three_parts_is_refused() -> None:
    with pytest.raises(ValueError, match="host\\|tenant\\|site"):
        WorkdayExtractor().board_url("acme")


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


async def test_a_single_page_board_is_read_in_one_request() -> None:
    fetcher, calls = _fetcher(total=5)

    pages = await WorkdayExtractor().collect(fetcher, SLUG)

    assert len(pages.postings) == 5
    assert calls == [0]


async def test_every_page_is_walked() -> None:
    """The failure this module exists to prevent.

    An extractor that read the first page would report a 95-role employer as
    having 20 — and on the next cycle `_close_missing` would close the other
    75, because they were absent from what it was shown.
    """
    fetcher, calls = _fetcher(total=95)

    pages = await WorkdayExtractor().collect(fetcher, SLUG)

    assert len(pages.postings) == 95
    assert calls == [0, 20, 40, 60, 80]
    assert len({p.external_id for p in pages.postings}) == 95


async def test_a_board_that_is_an_exact_multiple_of_the_page_stops() -> None:
    """Off-by-one: 40 postings must not cost a third, empty request."""
    fetcher, calls = _fetcher(total=40)

    pages = await WorkdayExtractor().collect(fetcher, SLUG)

    assert len(pages.postings) == 40
    assert calls == [0, 20]


async def test_an_empty_board_is_read_and_reported_empty() -> None:
    fetcher, _ = _fetcher(total=0)

    pages = await WorkdayExtractor().collect(fetcher, SLUG)

    assert pages.postings == []
    assert pages.truncated is False


# --------------------------------------------------------------------------
# Failure: a partial board is worse than none
# --------------------------------------------------------------------------


async def test_a_failed_page_abandons_the_whole_board() -> None:
    """A partial list is what `_close_missing` reads as "these have closed".

    One failed request is one failed board and the cycle goes on. One failed
    *page* silently halves a board, and the postings that were not shown get
    closed on the strength of it.
    """
    fetcher, _ = _fetcher(total=95, fail_page=2)

    with pytest.raises(WorkdayBoardError, match="HTTP 500"):
        await WorkdayExtractor().collect(fetcher, SLUG)


async def test_a_first_page_with_no_total_is_a_failure() -> None:
    """The total is what tells the walk when to stop.

    Without it the payload shape has changed, and continuing would mean
    guessing at the end of the board.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        return httpx.Response(200, text=json.dumps({"jobPostings": [_job(1)]}))

    clock = FakeClock()
    fetcher = PoliteFetcher(
        transport=httpx.MockTransport(handler),
        rate_limiter=HostRateLimiter(clock=clock, sleeper=clock.sleep),
    )

    with pytest.raises(WorkdayBoardError, match="no total"):
        await WorkdayExtractor().collect(fetcher, SLUG)


async def test_robots_is_still_obeyed() -> None:
    """A POST touches a host exactly as much as a GET does."""
    fetcher, _ = _fetcher(total=5, robots="User-agent: *\nDisallow: /")

    with pytest.raises(Blocked):
        await WorkdayExtractor().collect(fetcher, SLUG)


async def test_a_board_longer_than_max_pages_is_marked_truncated() -> None:
    """Reported, never silently halved.

    At §2.6's 60s floor a Workday tenant is its own host, so each page is a
    minute of waiting. `MAX_PAGES` bounds one company at an hour — and says
    so, because a board we know we did not finish reading must not be used to
    decide what has closed.
    """
    fetcher, calls = _fetcher(total=PAGE_SIZE * (MAX_PAGES + 5))

    pages = await WorkdayExtractor().collect(fetcher, SLUG)

    assert pages.truncated is True
    assert len(calls) == MAX_PAGES
    assert len(pages.postings) == PAGE_SIZE * MAX_PAGES


# --------------------------------------------------------------------------
# Fields
# --------------------------------------------------------------------------


def test_the_requisition_id_is_preferred_over_the_path() -> None:
    postings, total = parse_page(
        json.dumps({"total": 1, "jobPostings": [_job(7)]}), HOST, "AcmeCareers"
    )

    assert total == 1
    assert postings[0].external_id == "JR-00007"
    assert postings[0].url == f"https://{HOST}/AcmeCareers/job/San-Jose/Software-Engineer_JR-00007"


def test_an_id_is_recovered_from_the_path_when_bullet_fields_are_empty() -> None:
    entry = _job(9) | {"bulletFields": []}

    postings, _ = parse_page(json.dumps({"total": 1, "jobPostings": [entry]}), HOST, "AcmeCareers")

    assert postings[0].external_id == "JR-00009"


def test_a_posting_with_no_stable_id_is_skipped() -> None:
    """A synthetic id derived from the title changes when the title is edited,
    which reads as the old posting closing and a new one opening."""
    entry = {"title": "Engineer", "externalPath": "/job/Nowhere", "bulletFields": []}

    postings, _ = parse_page(json.dumps({"total": 1, "jobPostings": [entry]}), HOST, "AcmeCareers")

    assert postings == []


def test_the_relative_posted_date_is_not_turned_into_a_date() -> None:
    """ "Posted 30+ Days Ago" is prose. A date recovered from it would be
    invented precision, and `published_at` is the evidence that
    `poll_interval_s` is set sensibly."""
    postings, _ = parse_page(
        json.dumps({"total": 1, "jobPostings": [_job(1)]}), HOST, "AcmeCareers"
    )

    assert postings[0].published_at is None


def test_the_listing_carries_no_description() -> None:
    """Recorded as a fact, not worked around.

    Descriptions live behind one detail request per posting, which at 60s a
    request is eight hours for a 500-role employer, per cycle. The concrete
    consequence: `embed_postings` skips a posting with no description, so
    these score on title alone.
    """
    postings, _ = parse_page(
        json.dumps({"total": 1, "jobPostings": [_job(1)]}), HOST, "AcmeCareers"
    )

    assert postings[0].description_raw is None
    assert postings[0].ats_type == "workday"


def test_a_malformed_page_yields_nothing_rather_than_raising() -> None:
    assert parse_page("not json", HOST, "AcmeCareers") == ([], None)
    assert parse_page("[]", HOST, "AcmeCareers") == ([], None)


def test_workday_is_reachable_through_the_registry() -> None:
    """`crawl_company` finds every extractor through `extractor_for`."""
    extractor = extractor_for("workday")

    assert extractor is not None
    assert extractor.ats == "workday"
    assert hasattr(extractor, "collect"), "a paged board is read through collect"


# --------------------------------------------------------------------------
# Through crawl_company
# --------------------------------------------------------------------------


async def test_a_workday_board_stores_its_postings(db_session) -> None:
    from packages.crawler.crawl import crawl_company
    from packages.crawler.extract import CompanySeed

    fetcher, _ = _fetcher(total=45)
    seed = CompanySeed(name="Acme", slug=SLUG, ats="workday", poll_interval_s=0)

    result = await crawl_company(db_session, seed, fetcher, force=True)

    assert result.error is None
    assert result.fetched is True
    assert result.new_postings == 45, "every page, not just the first"


async def test_a_second_crawl_of_an_unchanged_workday_board_emits_nothing(db_session) -> None:
    """Gate 5's property, over a board that took three requests to read.

    The board hash covers every page, so an unchanged board short-circuits
    before any of them is parsed.
    """
    from packages.crawler.crawl import crawl_company
    from packages.crawler.extract import CompanySeed

    seed = CompanySeed(name="Acme", slug=SLUG, ats="workday", poll_interval_s=0)
    first, _ = _fetcher(total=45)
    await crawl_company(db_session, seed, first, force=True)

    second, _ = _fetcher(total=45)
    again = await crawl_company(db_session, seed, second, force=True)

    assert again.emitted == 0
    assert again.unchanged is True


async def test_a_truncated_board_closes_nothing(db_session) -> None:
    """The bulk-close this repo has already been bitten by, with a new excuse.

    A truncated read is a successful fetch that did not see the whole board.
    Treating "absent from what we read" as "closed" would drop the tail of
    every large Workday employer out of the feed, and the next full read would
    re-create them as new — churn that looks entirely normal in the audit
    trail.
    """
    from packages.crawler.crawl import crawl_company
    from packages.crawler.extract import CompanySeed

    seed = CompanySeed(name="Acme", slug=SLUG, ats="workday", poll_interval_s=0)
    full, _ = _fetcher(total=PAGE_SIZE * 2)
    await crawl_company(db_session, seed, full, force=True)

    huge, _ = _fetcher(total=PAGE_SIZE * (MAX_PAGES + 5))
    result = await crawl_company(db_session, seed, huge, force=True)

    assert result.suspect_parse is True
    assert result.closed_postings == 0


async def test_a_failed_page_is_reported_as_a_failed_company(db_session) -> None:
    """One bad board must not stop the cycle, and must not half-store."""
    from packages.crawler.crawl import crawl_company
    from packages.crawler.extract import CompanySeed

    fetcher, _ = _fetcher(total=95, fail_page=2)
    seed = CompanySeed(name="Acme", slug=SLUG, ats="workday", poll_interval_s=0)

    result = await crawl_company(db_session, seed, fetcher, force=True)

    assert result.error is not None
    assert result.new_postings == 0, "a board read in part is not stored in part"


async def test_a_bad_workday_slug_is_a_registry_problem_not_a_fetch(db_session) -> None:
    """Reported as a skip rather than attempted against a nonsense address."""
    from packages.crawler.crawl import crawl_company
    from packages.crawler.extract import CompanySeed

    fetcher, calls = _fetcher(total=5)
    seed = CompanySeed(name="Acme", slug="acme", ats="workday", poll_interval_s=0)

    result = await crawl_company(db_session, seed, fetcher, force=True)

    assert result.skipped_reason is not None
    assert "unusable slug" in result.skipped_reason
    assert calls == [], "nothing was fetched"
