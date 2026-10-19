"""The deep dive: a harder look at a noted company, and a sentence about it.

Each case is one found by reading 53 failed companies again by hand on
2026-10-19 (`packages/crawler/deep_dive.py`).
"""

from __future__ import annotations

from packages.crawler.deep_dive import deep_dive, read_links
from packages.crawler.find_boards import Resolved
from tests.test_discovery_careers_pages import _site

BOARD_JSON = '{"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "https://x/1"}]}'
GREENHOUSE_API = "boards-api.greenhouse.io/v1/boards/acme/jobs"


def _dns(*alive: str):
    async def resolver(host: str) -> bool:
        return host in alive

    return resolver


async def _dive(pages: dict[str, str], **kwargs):
    defaults = {
        "name": "Acme",
        "stage": "discovery",
        "reason": "r",
        "lead": "https://acme.com/",
    }
    return await deep_dive(fetcher=_site(pages), **{**defaults, **kwargs})


# --- a site that did not answer ------------------------------------------------------


async def test_a_domain_with_no_address_is_gone() -> None:
    found = await _dive({}, category="unreachable", resolver=_dns())

    assert found.board is None
    assert "no longer exists" in found.diagnosis
    assert found.findings["dns"] == {"acme.com": False, "www.acme.com": False}


async def test_a_site_answering_at_www_is_read_there() -> None:
    """Cavalry and Feedgen, 2026-10-19: the bare domain refused, www answered."""
    pages = {
        "www.acme.com/": '<a href="https://boards.greenhouse.io/acme">Jobs</a>',
        GREENHOUSE_API: BOARD_JSON,
    }

    found = await _dive(pages, category="unreachable", resolver=_dns("acme.com", "www.acme.com"))

    assert isinstance(found.board, Resolved)
    assert (found.board.ats, found.board.slug) == ("greenhouse", "acme")
    assert "answers at www.acme.com" in found.diagnosis


async def test_a_site_answering_at_www_without_a_board_says_both() -> None:
    pages = {"www.acme.com/": "<p>We make things.</p>"}

    found = await _dive(pages, category="unreachable", resolver=_dns("acme.com", "www.acme.com"))

    assert found.board is None
    assert found.diagnosis.startswith("its site answers at www.acme.com, not acme.com; ")
    assert found.fix.startswith("use https://www.acme.com/ as its website")
    assert found.kind == "other_address", "the sheet's address is what to fix first"


async def test_a_site_refusing_both_forms_is_left_alone() -> None:
    found = await _dive(
        {},
        category="unreachable",
        reason="its own site could not be fetched (HTTP 403); name-guessing is off for ...",
        resolver=_dns("acme.com", "www.acme.com"),
    )

    assert found.board is None
    assert "does not answer us at acme.com or www.acme.com (its own site could" in found.diagnosis
    assert "name-guessing" not in found.diagnosis
    assert "§2.5" in found.fix


# --- a careers page that named no board ----------------------------------------------


async def _careers(page: str, **more: str):
    return await _dive(
        {"acme.com/careers": page, **more},
        category="careers_page_unreadable",
        careers_page="https://acme.com/careers",
    )


async def test_a_board_one_page_further_in_is_found() -> None:
    found = await _careers(
        '<a href="https://jobs.acme.com/">See open roles</a>',
        **{
            "jobs.acme.com/": '<a href="https://boards.greenhouse.io/acme">All jobs</a>',
            GREENHOUSE_API: BOARD_JSON,
        },
    )

    assert isinstance(found.board, Resolved)
    assert found.board.slug == "acme"


async def test_a_job_system_one_page_further_in_is_named() -> None:
    """Boeing: its careers page links jobs.boeing.com, which links Workday."""
    found = await _careers(
        '<a href="https://jobs.acme.com/">Search jobs</a>',
        **{"jobs.acme.com/": '<a href="https://acme.wd1.myworkdayjobs.com/External">Go</a>'},
    )

    assert found.board is None
    assert found.diagnosis.startswith("its jobs are on Workday")
    assert found.fix == "add a reader for Workday"


async def test_a_page_sending_applicants_elsewhere_names_where() -> None:
    """CarbonRun: its job page links careers.risepeople.com, a system not read."""
    found = await _careers(
        '<a href="https://careers.risepeople.com/carbon-run/en/1">Apply</a>'
        '<a href="https://www.linkedin.com/company/acme">LinkedIn</a>'
        '<a href="https://twitter.com/acme">Follow</a>'
    )

    assert "sends applicants to careers.risepeople.com" in found.diagnosis


async def test_jobs_on_its_own_pages_are_said_to_be() -> None:
    found = await _careers(
        '<a href="/careers/forward-deployed-engineer">Engineer</a>'
        '<a href="/careers/account-manager">Account manager</a>'
    )

    assert found.diagnosis.startswith("its careers page lists 2 jobs on its own pages")


async def test_applications_by_email_are_said_to_be() -> None:
    """Fielo and Mira: a mailto link to hr@ or careers@ and no board."""
    found = await _careers('<a href="mailto:hr@acme.com?subject=Job%20Application">Apply</a>')

    assert found.diagnosis == "it takes applications by email (hr@acme.com)"


async def test_a_page_with_nothing_on_it_is_said_to_need_a_browser() -> None:
    found = await _careers('<div id="root"></div><script src="/app.js"></script>')

    assert "without running its JavaScript" in found.diagnosis


# --- a site with no careers page -----------------------------------------------------


async def test_a_careers_link_to_another_site_is_named_not_followed() -> None:
    """Bugsnag links SmartBear's careers page: likely its parent company."""
    pages = {"acme.com/": '<a href="https://smartbear.com/company/careers/">Careers</a>'}

    found = await _dive(pages, category="no_careers_page")

    assert found.diagnosis.startswith("its site sends applicants to smartbear.com")
    assert found.board is None


async def test_a_site_with_nothing_may_not_be_hiring() -> None:
    found = await _dive({"acme.com/": "<p>Hello</p>"}, category="no_careers_page")

    assert "may not be hiring" in found.diagnosis


# --- nothing more to try -------------------------------------------------------------


async def test_another_job_system_goes_straight_to_the_error_list() -> None:
    found = await _dive(
        {},
        category="other_job_system",
        reason="careers page x uses Workday (y), which is not a supported board yet; "
        "name-guessing is off for a company with a website",
    )

    assert found.diagnosis == "careers page x uses Workday (y), which is not a supported board yet"
    assert found.fix == "add a reader for that job system"


# --- a known board that stopped answering --------------------------------------------


async def test_a_board_that_moved_is_found_on_the_site() -> None:
    pages = {
        "acme.com/": '<a href="https://boards.greenhouse.io/acme">Jobs</a>',
        GREENHOUSE_API: BOARD_JSON,
    }

    found = await _dive(
        pages, stage="crawl", category="board_gone", reason="HTTP 404", board=("lever", "acme")
    )

    assert isinstance(found.board, Resolved)
    assert found.diagnosis == "lever/acme no longer exists; its site now names greenhouse/acme"


async def test_a_board_gone_with_nothing_on_the_site_says_so() -> None:
    found = await _dive(
        {"acme.com/": "<p>Hello</p>"},
        stage="crawl",
        category="board_gone",
        reason="HTTP 404",
        board=("lever", "acme"),
    )

    assert found.board is None
    assert found.diagnosis.startswith("lever/acme no longer exists, and its site names no other")


async def test_a_board_read_as_empty_points_at_the_reader() -> None:
    found = await _dive(
        {}, stage="crawl", category="board_reads_empty", reason="r", board=("ashby", "acme")
    )

    assert "reader is probably out of date" in found.diagnosis
    assert found.fix == "check the ashby reader against the board"


# --- reading a page's links ----------------------------------------------------------


def test_files_and_plumbing_are_not_job_links() -> None:
    page = read_links(
        '<a href="/careers/brochure.pdf">x</a>'
        '<a href="/wp-json/oembed/1.0/embed?url=careers">x</a>'
        '<a href="/careers.md">x</a>',
        "https://acme.com/careers",
    )

    assert (page.own_postings, page.own_job_links, page.other_sites) == ((), (), ())
