"""The per-company discovery task: directory first, then the site, guessing last.

Driven by the 200-company trial of the owner's sheet (docs/ATS_DISCOVERY_RESEARCH.md):
name-guessing found none of the 188 sheet companies and was nearly all of the
wall clock, while the public directories proposed 70% of the boards the trial
found without a request. These are the first tests this handler has had.
"""

from __future__ import annotations

import contextlib

import pytest
from sqlalchemy import func, select

from apps.worker import discover_company_job as job
from packages.core.enums import SourceStatus
from packages.core.models import Company, QueueTask
from packages.core.queue import ClaimedTask
from packages.crawler.board_directory import BoardDirectory, DirectoryEntry
from packages.crawler.dispatch import CRAWL_COMPANY_TASK_KIND
from packages.crawler.extract import ExtractedPosting
from packages.crawler.find_boards import Resolved, SiteReading


@pytest.fixture
def calls(monkeypatch) -> dict:
    """Fakes for every outbound step, recording what the handler asked for."""
    seen: dict = {"probes": [], "resolve": [], "reading": SiteReading(read=True)}

    @contextlib.asynccontextmanager
    async def fake_fetcher():
        yield object()

    async def no_defer(*args, **kwargs) -> None:
        return None

    async def fake_resolve(name, fetcher, *, url=None, vendors=(), guess=True, accept=None):
        seen["resolve"].append({"name": name, "url": url, "guess": guess})
        return (name, "no board found on any supported ATS"), seen["reading"]

    monkeypatch.setattr(job, "build_fetcher", fake_fetcher)
    monkeypatch.setattr(job, "defer_if_host_busy", no_defer)
    monkeypatch.setattr(job, "resolve_site", fake_resolve)
    monkeypatch.setattr(job, "load_directory", lambda: None)
    return seen


def _directory(*entries: tuple[str, str, str]) -> BoardDirectory:
    return BoardDirectory(DirectoryEntry(vendor=v, slug=s, company=c) for v, s, c in entries)


def _board_returning(seen: dict, jobs: int, text: str = "We build robots."):
    async def fake_board(fetcher, vendor, slug):
        seen["probes"].append((vendor, slug))
        postings = [
            ExtractedPosting(external_id=str(i), url=f"https://x/{i}", description_raw=text)
            for i in range(jobs)
        ]
        return postings, f"https://api.example/{vendor}/{slug}", None

    return fake_board


async def _company(session, *, website: str | None) -> Company:
    company = Company(
        name="Acme Robotics",
        supplied_website=website,
        source_status=SourceStatus.HINT.value,
    )
    session.add(company)
    await session.flush()
    return company


def _claim(company: Company) -> ClaimedTask:
    task = QueueTask(
        kind=job.DISCOVER_COMPANY_TASK_KIND, payload_json={"company_id": str(company.id)}
    )
    return ClaimedTask(task=task, reclaimed=False, previous_owner=None)


async def _crawls(session) -> int:
    return await session.scalar(
        select(func.count()).select_from(QueueTask).where(QueueTask.kind == CRAWL_COMPANY_TASK_KIND)
    )


async def test_a_directory_board_is_verified_without_reading_the_site(
    db_session, calls, monkeypatch
) -> None:
    monkeypatch.setattr(
        job, "load_directory", lambda: _directory(("ashby", "acme", "Acme Robotics"))
    )
    monkeypatch.setattr(job, "fetch_board", _board_returning(calls, jobs=4))
    company = await _company(db_session, website="https://acme.com")

    await job.handle_discover_company(db_session, _claim(company))

    assert company.source_status == SourceStatus.VERIFIED.value
    assert (company.ats_type, company.slug) == ("ashby", "acme")
    assert company.source_evidence["method"] == "discovery_from_directory"
    assert company.source_evidence["why"] == "slug matches website"
    assert company.source_evidence["confirmed_by"] == "slug is the website's .com name"
    assert calls["resolve"] == [], "the site and the guesses are not needed"
    assert await _crawls(db_session) == 1, "the board's first fetch is scheduled"


async def test_a_namesake_board_falls_through_and_is_recorded(
    db_session, calls, monkeypatch
) -> None:
    """Axle (axlepayments.com) was given Axle Informatics' board on 2026-10-05."""
    monkeypatch.setattr(
        job, "load_directory", lambda: _directory(("greenhouse", "acme", "Acme Robotics"))
    )
    monkeypatch.setattr(
        job,
        "fetch_board",
        _board_returning(calls, jobs=11, text="Acme Robotics is a bioscience company."),
    )
    # The site's label says "Acme Robotics Finance": a longer name than the match.
    company = await _company(db_session, website="https://acmeroboticsfinance.com")

    await job.handle_discover_company(db_session, _claim(company))

    assert company.source_status == SourceStatus.FAILED.value
    assert len(calls["resolve"]) == 1, "the company's own site is still read"
    assert company.source_evidence["unconfirmed"] == ["greenhouse/acme"]


async def test_a_name_match_whose_board_names_the_website_is_verified(
    db_session, calls, monkeypatch
) -> None:
    monkeypatch.setattr(
        job, "load_directory", lambda: _directory(("lever", "acme-hq", "Acme Robotics"))
    )
    monkeypatch.setattr(
        job,
        "fetch_board",
        _board_returning(calls, jobs=2, text="Learn more at https://acmerobotics.io/about"),
    )
    company = await _company(db_session, website="https://acmerobotics.io")

    await job.handle_discover_company(db_session, _claim(company))

    assert (company.ats_type, company.slug) == ("lever", "acme-hq")
    assert company.source_evidence["why"] == "same name in directory"
    assert company.source_evidence["confirmed_by"] == "board names the website"


async def test_a_directory_board_with_no_open_jobs_falls_through_to_the_site(
    db_session, calls, monkeypatch
) -> None:
    """A 200 with an empty list is usually a slug nobody owns."""
    monkeypatch.setattr(
        job, "load_directory", lambda: _directory(("ashby", "acme", "Acme Robotics"))
    )
    monkeypatch.setattr(job, "fetch_board", _board_returning(calls, jobs=0))
    company = await _company(db_session, website="https://acme.com")

    await job.handle_discover_company(db_session, _claim(company))

    assert calls["probes"] == [("ashby", "acme")]
    assert len(calls["resolve"]) == 1
    assert company.source_status == SourceStatus.FAILED.value


async def test_a_company_with_a_website_is_not_name_guessed(db_session, calls) -> None:
    """0 of 188 sheet companies were found by guessing; it was the whole wait."""
    company = await _company(db_session, website="https://acme.com")

    await job.handle_discover_company(db_session, _claim(company))

    assert calls["resolve"] == [
        {"name": "Acme Robotics", "url": "https://acme.com", "guess": False}
    ]


async def test_a_company_without_a_website_is_still_name_guessed(db_session, calls) -> None:
    """The trial's 5 guess successes were registry companies with no website."""
    company = await _company(db_session, website=None)

    await job.handle_discover_company(db_session, _claim(company))

    assert calls["resolve"][0]["guess"] is True


@pytest.mark.parametrize(("policy", "guess"), [("always", True), ("never", False)])
async def test_the_guessing_policy_is_a_setting(
    db_session, calls, monkeypatch, policy: str, guess: bool
) -> None:
    from packages.core.config import get_settings

    monkeypatch.setenv("CRAWLER_NAME_GUESSING", policy)
    get_settings.cache_clear()
    try:
        company = await _company(db_session, website="https://acme.com")
        await job.handle_discover_company(db_session, _claim(company))
    finally:
        get_settings.cache_clear()

    assert calls["resolve"][0]["guess"] is guess


def test_should_guess_reads_the_policy() -> None:
    assert job.should_guess(lead=None, policy="no_website") is True
    assert job.should_guess(lead="https://acme.com", policy="no_website") is False
    assert job.should_guess(lead="https://acme.com", policy="always") is True
    assert job.should_guess(lead=None, policy="never") is False


def test_an_unknown_policy_is_refused(monkeypatch) -> None:
    from pydantic import ValidationError

    from packages.core.config import Settings

    monkeypatch.setenv("CRAWLER_NAME_GUESSING", "sometimes")
    with pytest.raises(ValidationError):
        Settings()


# --- a board somebody already holds ---------------------------------------------
#
# The owner's sheet lists a company under an old name and a new one. Both
# resolve to the same board, and verifying both stored every posting on eleven
# boards twice (`packages/crawler/duplicate_boards.py`).


async def _holder(session) -> Company:
    holder = Company(
        name="Acme",
        supplied_website="https://acme.com",
        ats_type="ashby",
        slug="acme",
        source_status=SourceStatus.VERIFIED.value,
    )
    session.add(holder)
    await session.flush()
    return holder


async def test_a_board_another_company_holds_is_not_verified_twice(
    db_session, calls, monkeypatch
) -> None:
    monkeypatch.setattr(
        job, "load_directory", lambda: _directory(("ashby", "acme", "Acme Robotics"))
    )
    monkeypatch.setattr(job, "fetch_board", _board_returning(calls, jobs=4))
    holder = await _holder(db_session)
    company = await _company(db_session, website="https://acme.com")

    await job.handle_discover_company(db_session, _claim(company))

    assert company.source_status == SourceStatus.FAILED.value
    assert company.slug is None
    assert company.source_evidence["method"] == "duplicate_board"
    assert company.source_evidence["duplicate_of"] == str(holder.id)
    assert "Acme" in (company.discovery_failure or "")
    assert await _crawls(db_session) == 0, "the board is already being polled"


async def test_losing_a_race_for_a_board_ends_the_same_way(db_session, calls, monkeypatch) -> None:
    """Two tasks can resolve one board at once, each before the other commits.

    Neither then finds a holder. Reproduced by answering "nobody" the first
    time the handler asks: the write is what meets the other row, at the
    index, and the handler has to come out of that with the company set aside
    rather than with a failed task.
    """
    monkeypatch.setattr(
        job, "load_directory", lambda: _directory(("ashby", "acme", "Acme Robotics"))
    )
    monkeypatch.setattr(job, "fetch_board", _board_returning(calls, jobs=4))
    holder = await _holder(db_session)
    company = await _company(db_session, website="https://acme.com")
    real, asked = job.holder_of, []

    async def nobody_at_first(session, ats, slug, *, other_than):
        asked.append((ats, slug))
        if len(asked) <= 2:
            return None
        return await real(session, ats, slug, other_than=other_than)

    monkeypatch.setattr(job, "holder_of", nobody_at_first)

    await job.handle_discover_company(db_session, _claim(company))

    assert len(asked) == 3
    assert company.source_status == SourceStatus.FAILED.value
    assert company.source_evidence["duplicate_of"] == str(holder.id)
    assert await _crawls(db_session) == 0
    assert holder.source_status == SourceStatus.VERIFIED.value


# --- what a failure records ----------------------------------------------------------


async def test_a_failure_keeps_the_careers_page_and_the_fix_it_needs(db_session, calls) -> None:
    """The owner's sheet gave Google search links for careers pages. A careers
    page found on the company's own site is often the only real one the
    registry has, and was thrown away with the failure."""
    calls["reading"] = SiteReading(
        read=True,
        careers_page="https://acme.com/careers",
        job_system="Workday",
        job_system_url="https://acme.wd5.myworkdayjobs.com/External",
    )
    company = await _company(db_session, website="https://acme.com")

    await job.handle_discover_company(db_session, _claim(company))

    assert company.source_status == SourceStatus.FAILED.value
    evidence = company.source_evidence
    assert evidence["category"] == "other_job_system"
    assert evidence["careers_page"] == "https://acme.com/careers"
    assert evidence["job_system"] == "Workday"
    assert evidence["job_system_url"] == "https://acme.wd5.myworkdayjobs.com/External"
    assert "structured_jobs" not in evidence, "only what was found is recorded"


async def test_a_site_with_no_careers_page_records_what_was_tried(db_session, calls) -> None:
    calls["reading"] = SiteReading(
        read=True, tried=("https://acme.com/careers", "https://acme.com/jobs")
    )
    company = await _company(db_session, website="https://acme.com")

    await job.handle_discover_company(db_session, _claim(company))

    assert company.source_evidence["category"] == "no_careers_page"
    assert company.source_evidence["tried"] == [
        "https://acme.com/careers",
        "https://acme.com/jobs",
    ]


async def test_a_company_with_no_website_is_recorded_as_having_none(db_session, calls) -> None:
    company = await _company(db_session, website=None)

    await job.handle_discover_company(db_session, _claim(company))

    assert company.source_evidence["category"] == "no_website"


async def test_a_portfolio_board_records_the_boards_it_named(db_session, calls) -> None:
    """GGV Capital, 2026-10-19: its careers link was a Getro board listing other
    companies' jobs. The boards are kept so a person can see why."""
    calls["reading"] = SiteReading(
        read=True,
        careers_page="https://jobs.notablecap.com/jobs",
        boards_named=("ashby/ibotta", "lever/cyderes"),
        network_board="Getro",
    )
    company = await _company(db_session, website="https://www.notablecap.com")

    await job.handle_discover_company(db_session, _claim(company))

    evidence = company.source_evidence
    assert company.source_status == SourceStatus.FAILED.value
    assert evidence["category"] == "multi_company_board"
    assert evidence["boards_named"] == ["ashby/ibotta", "lever/cyderes"]
    assert evidence["network_board"] == "Getro"


# --- a failure is noted for the deep dive --------------------------------------------


async def _open_issues(session, company) -> list:
    from packages.core.models_issues import CompanyIssue

    rows = await session.scalars(
        select(CompanyIssue)
        .where(CompanyIssue.company_id == company.id)
        .execution_options(populate_existing=True)
    )
    return list(rows.all())


async def test_a_failure_is_noted_and_the_worker_moves_on(db_session, calls) -> None:
    calls["reading"] = SiteReading(read=True, careers_page="https://acme.com/careers")
    company = await _company(db_session, website="https://acme.com")

    await job.handle_discover_company(db_session, _claim(company))

    [issue] = await _open_issues(db_session, company)
    assert (issue.stage, issue.category, issue.status) == (
        "discovery",
        "careers_page_unreadable",
        "noted",
    )
    assert issue.reason == company.discovery_failure


async def test_a_board_listing_nothing_is_not_an_issue(db_session, calls, monkeypatch) -> None:
    async def empty_board(name, fetcher, *, url=None, vendors=(), guess=True, accept=None):
        return (name, "board found (lever/acme) but it lists no open roles"), SiteReading(
            board=("lever", "acme"), read=True
        )

    monkeypatch.setattr(job, "resolve_site", empty_board)
    company = await _company(db_session, website="https://acme.com")

    await job.handle_discover_company(db_session, _claim(company))

    assert await _open_issues(db_session, company) == []


async def test_a_verification_closes_the_note(db_session, calls, monkeypatch) -> None:
    company = await _company(db_session, website="https://acme.com")
    await job.handle_discover_company(db_session, _claim(company))

    async def found(name, fetcher, *, url=None, vendors=(), guess=True, accept=None):
        board = Resolved(
            name=name, ats="lever", slug="acme", board_url="https://jobs.lever.co/acme", open_jobs=3
        )
        return board, SiteReading(board=("lever", "acme"), read=True)

    monkeypatch.setattr(job, "resolve_site", found)
    await job.handle_discover_company(db_session, _claim(company))

    [issue] = await _open_issues(db_session, company)
    assert (issue.status, issue.resolved_by) == ("fixed", "discovery")


async def test_collision_keeps_searching_other_guessed_slugs(
    db_session, calls, monkeypatch
) -> None:
    from packages.crawler import find_boards

    holder = Company(
        name="Juniper",
        supplied_website="https://juniper.example",
        ats_type="ashby",
        slug="juniper",
        source_status="verified",
    )
    target = Company(name="Juniper Labs", source_status="hint")
    db_session.add_all([holder, target])
    await db_session.flush()
    monkeypatch.setattr(job, "resolve_site", find_boards.resolve_site)
    monkeypatch.setattr(find_boards, "slug_candidates", lambda name: ["juniper", "juniper-labs"])

    async def probe(fetcher, vendor, slug):
        return (2 if vendor == "ashby" else None), f"https://jobs.example/{slug}", None

    monkeypatch.setattr(find_boards, "_probe", probe)

    await job.handle_discover_company(db_session, _claim(target))

    assert target.source_status == "verified"
    assert target.slug == "juniper-labs"
    assert target.source_evidence["unconfirmed"] == ["ashby/juniper"]
    assert target.source_evidence["name_collisions"][0]["holder_name"] == "Juniper"
    assert await _crawls(db_session) == 1


async def test_collision_keeps_searching_other_directory_candidates(
    db_session, calls, monkeypatch
) -> None:
    db_session.add(
        Company(
            name="Juniper",
            supplied_website="https://juniper.example",
            ats_type="ashby",
            slug="juniper",
            source_status="verified",
        )
    )
    target = Company(name="Juniper Labs", source_status="hint")
    db_session.add(target)
    await db_session.flush()
    monkeypatch.setattr(
        job,
        "load_directory",
        lambda: _directory(
            ("ashby", "juniper", "Juniper Labs"), ("lever", "juniper-labs", "Juniper Labs")
        ),
    )
    monkeypatch.setattr(job, "fetch_board", _board_returning(calls, jobs=2))
    await job.handle_discover_company(db_session, _claim(target))
    assert target.source_status == "verified"
    assert target.slug == "juniper-labs"
    assert target.source_evidence["unconfirmed"] == ["ashby/juniper"]


async def test_current_own_site_link_is_identity_before_candidate_filtering(
    db_session, calls, monkeypatch
) -> None:
    from packages.crawler import find_boards

    holder = Company(
        name="Canopy",
        supplied_website="https://canopy.example",
        ats_type="ashby",
        slug="canopy",
        source_status="verified",
    )
    target = Company(
        name="Old Finch",
        domain="oldfinch.example",
        supplied_website="https://oldfinch.example",
        source_status="hint",
    )
    db_session.add_all([holder, target])
    await db_session.flush()
    monkeypatch.setattr(job, "resolve_site", find_boards.resolve_site)

    async def own_page(url, fetcher):
        assert url == target.supplied_website
        return SiteReading(board=("ashby", "canopy"), read=True)

    async def probe(fetcher, vendor, slug):
        return 2, "https://jobs.ashbyhq.com/canopy", None

    monkeypatch.setattr(find_boards, "from_url", own_page)
    monkeypatch.setattr(find_boards, "_probe", probe)

    await job.handle_discover_company(db_session, _claim(target))

    assert target.source_evidence["method"] == "duplicate_board"
    assert target.source_evidence["duplicate_of"] == str(holder.id)
    assert "own website" in target.source_evidence["identity_reason"]
    assert await _crawls(db_session) == 0


async def test_directory_candidate_cannot_borrow_the_current_own_site_lead(
    db_session, calls, monkeypatch
) -> None:
    db_session.add(
        Company(
            name="Canopy",
            supplied_website="https://canopy.example",
            ats_type="ashby",
            slug="canopy",
            source_status="verified",
        )
    )
    target = Company(
        name="Old Finch",
        domain="oldfinch.example",
        supplied_website="https://oldfinch.example",
        source_status="hint",
    )
    db_session.add(target)
    await db_session.flush()
    monkeypatch.setattr(job, "load_directory", lambda: _directory(("ashby", "canopy", "Old Finch")))
    monkeypatch.setattr(job, "fetch_board", _board_returning(calls, jobs=2))

    await job.handle_discover_company(db_session, _claim(target))

    assert target.source_evidence["category"] == "name_collision"
    assert target.source_evidence["unconfirmed"] == ["ashby/canopy"]
    assert "duplicate_of" not in target.source_evidence
    assert calls["resolve"]


async def test_name_guess_after_reading_a_site_is_not_recorded_as_url_proof(
    db_session, calls, monkeypatch
) -> None:
    from packages.crawler import find_boards

    target = Company(
        name="Old Finch",
        domain="oldfinch.example",
        supplied_website="https://oldfinch.example",
        source_status="hint",
    )
    db_session.add(target)
    await db_session.flush()
    settings = job.get_settings().model_copy(
        update={"crawler_name_guessing": "always", "crawler_discovery_vendors": "ashby"}
    )
    monkeypatch.setattr(job, "get_settings", lambda: settings)
    monkeypatch.setattr(job, "resolve_site", find_boards.resolve_site)
    monkeypatch.setattr(find_boards, "slug_candidates", lambda name: ["canopy"])

    async def no_board_on_site(url, fetcher):
        return SiteReading(read=True)

    async def probe(fetcher, vendor, slug):
        return 2, "https://jobs.ashbyhq.com/canopy", None

    monkeypatch.setattr(find_boards, "from_url", no_board_on_site)
    monkeypatch.setattr(find_boards, "_probe", probe)

    await job.handle_discover_company(db_session, _claim(target))

    assert target.source_status == "verified"
    assert target.source_evidence["method"] == "discovery_from_name"
