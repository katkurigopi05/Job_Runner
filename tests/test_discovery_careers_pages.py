"""Discovery finds a company's careers page, and says what it found there.

On 2026-10-19 3,684 of the 4,251 companies on the owner's sheet were failed
by discovery, nearly all as "its own site names no supported board". Its
careers column was 3,797 Google search links, so the company's own site was
the only lead. A sample of 40 of those companies, read by hand through the
polite fetcher, found a careers page for 26: one on a supported board that
discovery had missed, five on another job system (Workday, BambooHR,
Paylocity, SuccessFactors), twenty on the company's own page. Discovery threw
all of that away: it followed a careers link only on the exact same host,
never tried `/careers` when the home page linked none, and recorded only that
no supported board was named.
"""

from __future__ import annotations

import httpx
import pytest

from packages.crawler.fetch import PoliteFetcher
from packages.crawler.find_boards import (
    COMMON_CAREERS_PATHS,
    Resolved,
    careers_links,
    from_url,
    resolve_site,
)
from packages.crawler.job_systems import other_job_system
from packages.crawler.ratelimit import HostRateLimiter


def _site(pages: dict[str, str], requested: list[str] | None = None) -> PoliteFetcher:
    """Serve `pages` by exact `host + path`; robots allows everything; 404 otherwise."""

    def handler(request: httpx.Request) -> httpx.Response:
        if requested is not None and request.url.path != "/robots.txt":
            requested.append(f"{request.url.host}{request.url.path}")
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        key = f"{request.url.host}{request.url.path}"
        if key in pages:
            return httpx.Response(200, text=pages[key])
        return httpx.Response(404, text="")

    # A fake clock: the 60s floor between requests to one host still holds,
    # and the test does not sleep through it.
    from tests.test_crawler import FakeClock

    clock = FakeClock()
    limiter = HostRateLimiter(clock=clock, sleeper=clock.sleep)
    return PoliteFetcher(transport=httpx.MockTransport(handler), rate_limiter=limiter)


WORKDAY = '<a href="https://acme.wd5.myworkdayjobs.com/External">See open roles</a>'
JSONLD = (
    '<script type="application/ld+json">{"@context": "https://schema.org", '
    '"@type": "JobPosting", "title": "Engineer", "datePosted": "2026-10-01"}</script>'
)


# --- following a careers link -------------------------------------------------------


def test_a_careers_link_on_a_subdomain_of_the_same_site_is_followed() -> None:
    html = (
        '<a href="https://careers.acme.com/">Careers</a>'
        '<a href="https://jobs.acme.com/openings">Jobs</a>'
        '<a href="https://acme.com.evil.net/careers">Careers</a>'
        '<a href="https://other.com/careers">Careers</a>'
    )

    assert careers_links(html, "https://www.acme.com/") == [
        "https://careers.acme.com/",
        "https://jobs.acme.com/openings",
    ]


def test_a_two_part_country_suffix_is_one_site_not_every_site() -> None:
    """`co.uk` is not a company: two companies under it are two sites."""
    html = '<a href="https://careers.other.co.uk/">Careers</a>'

    assert careers_links(html, "https://acme.co.uk/") == []


# --- trying the usual addresses -----------------------------------------------------


async def test_a_home_page_that_links_no_careers_page_has_careers_tried() -> None:
    requested: list[str] = []
    fetcher = _site(
        {"acme.com/": "<p>About us.</p>", "acme.com/careers": "<h1>Join</h1>"}, requested
    )

    reading = await from_url("https://acme.com/", fetcher)

    assert reading.careers_page == "https://acme.com/careers"
    assert requested == ["acme.com/", "acme.com/careers"], "stops at the first that exists"


async def test_jobs_is_tried_when_careers_does_not_exist() -> None:
    fetcher = _site({"acme.com/": "<p>About us.</p>", "acme.com/jobs": "<h1>Openings</h1>"})

    reading = await from_url("https://acme.com/", fetcher)

    assert COMMON_CAREERS_PATHS[:2] == ("/careers", "/jobs")
    assert reading.careers_page == "https://acme.com/jobs"


async def test_the_usual_addresses_are_not_tried_when_the_page_links_its_careers() -> None:
    requested: list[str] = []
    fetcher = _site(
        {"acme.com/": '<a href="/work-with-us">Work with us</a>', "acme.com/work-with-us": "Hi"},
        requested,
    )

    reading = await from_url("https://acme.com/", fetcher)

    assert reading.careers_page == "https://acme.com/work-with-us"
    assert requested == ["acme.com/", "acme.com/work-with-us"]


async def test_a_site_with_no_careers_page_says_what_was_tried() -> None:
    fetcher = _site({"acme.com/": "<p>About us.</p>"})

    reading = await from_url("https://acme.com/", fetcher)

    assert reading.careers_page is None
    assert reading.tried == ("https://acme.com/careers", "https://acme.com/jobs")


# --- what the careers page is built on ----------------------------------------------


def test_another_job_system_is_named_by_a_link_to_it() -> None:
    found = other_job_system(WORKDAY)

    assert found == ("Workday", "https://acme.wd5.myworkdayjobs.com/External")


def test_a_job_system_named_in_prose_is_not_a_link_to_one() -> None:
    """A page saying "we moved off Workday" has not linked a Workday board."""
    assert other_job_system("<p>We moved our payroll off Workday and ADP.</p>") is None


def test_a_lookalike_host_is_not_the_job_system() -> None:
    assert other_job_system('<a href="https://myworkdayjobs.com.evil.net/x">Jobs</a>') is None


async def test_a_careers_page_on_another_job_system_is_recorded() -> None:
    fetcher = _site({"acme.com/": '<a href="/careers">Careers</a>', "acme.com/careers": WORKDAY})

    reading = await from_url("https://acme.com/", fetcher)

    assert reading.careers_page == "https://acme.com/careers"
    assert reading.job_system == "Workday"
    assert reading.job_system_url == "https://acme.wd5.myworkdayjobs.com/External"


async def test_structured_jobs_on_a_careers_page_are_counted() -> None:
    fetcher = _site({"acme.com/": '<a href="/careers">Careers</a>', "acme.com/careers": JSONLD})

    reading = await from_url("https://acme.com/", fetcher)

    assert reading.structured_jobs == 1


# --- what discovery says when it cannot read the jobs -------------------------------


@pytest.mark.parametrize(
    ("pages", "category", "words"),
    [
        (
            {"acme.com/": '<a href="/careers">Careers</a>', "acme.com/careers": WORKDAY},
            "other_job_system",
            ["https://acme.com/careers", "Workday", "not a supported board"],
        ),
        (
            {"acme.com/": '<a href="/careers">Careers</a>', "acme.com/careers": JSONLD},
            "structured_jobs",
            ["https://acme.com/careers", "1 job", "structured data"],
        ),
        (
            {"acme.com/": "<p>About us.</p>", "acme.com/careers": "<h1>We are hiring</h1>"},
            "careers_page_unreadable",
            ["https://acme.com/careers", "names no supported board"],
        ),
        (
            {"acme.com/": "<p>About us.</p>"},
            "no_careers_page",
            ["no careers page", "/careers", "/jobs"],
        ),
    ],
)
async def test_a_failure_says_what_was_found_and_how_to_fix_it(
    pages: dict[str, str], category: str, words: list[str]
) -> None:
    outcome, reading = await resolve_site(
        "Acme", _site(pages), url="https://acme.com/", guess=False
    )

    assert not isinstance(outcome, Resolved)
    assert reading.category == category
    for word in words:
        assert word in outcome[1]


def test_a_robots_file_that_says_no_is_blocked_and_one_never_read_is_unreachable() -> None:
    from packages.crawler.find_boards import SiteReading

    refused = SiteReading(blocked="https://acme.com/careers disallowed by robots.txt")
    unread = SiteReading(blocked="https://acme.com: robots.txt could not be read; refusing...")

    assert refused.category == "blocked"
    assert unread.category == "unreachable"


# --- a page that lists other companies' jobs ----------------------------------------
#
# Found on 2026-10-19 by the first recheck with the code above. GGV Capital's
# site (notablecap.com) links jobs.notablecap.com, a Getro job board listing
# jobs at the firms it invests in: Ibotta and Brightwheel on Ashby, Cyderes on
# Lever, Wiz on Greenhouse. Discovery took the first of them, Cyderes, as GGV's
# own board, and twelve Cyderes postings were stored under GGV Capital.

UUID_A = "2b3c4d5e-6f70-4a81-9b2c-3d4e5f607182"
UUID_B = "8a9b0c1d-2e3f-4a5b-8c7d-9e0f1a2b3c4d"


def _lever(slug: str, posting: str = UUID_A) -> str:
    return f'<a href="https://jobs.lever.co/{slug}/{posting}">A role</a>'


PORTFOLIO = (
    _lever("cyderes")
    + _lever("cyderes", UUID_B)
    + '<a href="https://boards.greenhouse.io/otherco/jobs/3333">Designer, Otherco</a>'
)
GETRO = '<img src="https://cdn.getro.com/collections/1a70bb9b.png">'


def _venture_firm(jobs_page: str) -> PoliteFetcher:
    """A firm whose home page links a jobs subdomain serving `jobs_page`."""
    return _site({"vc.com/": '<a href="https://jobs.vc.com/">Jobs</a>', "jobs.vc.com/": jobs_page})


async def test_a_page_listing_several_companies_boards_names_none_of_them() -> None:
    fetcher = _venture_firm(PORTFOLIO)

    reading = await from_url("https://vc.com/", fetcher)

    assert reading.board is None
    assert reading.boards_named == ("lever/cyderes", "greenhouse/otherco")
    assert reading.category == "multi_company_board"


async def test_a_home_page_listing_several_companies_boards_names_none_either() -> None:
    reading = await from_url("https://vc.com/", _site({"vc.com/": PORTFOLIO}))

    assert reading.board is None
    assert reading.category == "multi_company_board"


async def test_several_jobs_on_one_board_still_name_that_board() -> None:
    page = _lever("acme") + _lever("acme", UUID_B)
    fetcher = _site({"acme.com/": '<a href="/careers">Careers</a>', "acme.com/careers": page})

    reading = await from_url("https://acme.com/", fetcher)

    assert reading.board == ("lever", "acme")


async def test_one_company_on_two_systems_is_still_one_company() -> None:
    """A company that moved systems and left a link to the old board up."""
    page = _lever("acme") + '<a href="https://boards.greenhouse.io/acme">Old board</a>'

    reading = await from_url("https://acme.com/careers", _site({"acme.com/careers": page}))

    assert reading.board == ("lever", "acme"), "a posting link outranks a front page, as before"


async def test_among_several_boards_the_one_named_like_the_site_is_its_own() -> None:
    """`acme.com` naming its own board and a partner's: the page and the
    company's own address agree on one of them."""
    page = _lever("partnerco") + '<a href="https://jobs.ashbyhq.com/acme">Our jobs</a>'

    reading = await from_url("https://careers.acme.com/", _site({"careers.acme.com/": page}))

    assert reading.board == ("ashby", "acme")


async def test_a_getro_board_is_never_one_companys_board() -> None:
    """A portfolio board whose first screen happens to show one company's jobs
    is still a portfolio board."""
    fetcher = _venture_firm(GETRO + _lever("cyderes"))

    reading = await from_url("https://vc.com/", fetcher)

    assert reading.board is None
    assert reading.network_board == "Getro"
    assert reading.category == "multi_company_board"


async def test_a_portfolio_page_says_so_in_the_reason() -> None:
    fetcher = _venture_firm(PORTFOLIO)

    outcome, _ = await resolve_site("VC", fetcher, url="https://vc.com/", guess=False)

    assert not isinstance(outcome, Resolved)
    reason = outcome[1]
    assert "https://jobs.vc.com/" in reason
    assert "2 different companies" in reason
    assert "lever/cyderes" in reason
