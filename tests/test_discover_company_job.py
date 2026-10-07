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


@pytest.fixture
def calls(monkeypatch) -> dict:
    """Fakes for every outbound step, recording what the handler asked for."""
    seen: dict = {"probes": [], "resolve": []}

    @contextlib.asynccontextmanager
    async def fake_fetcher():
        yield object()

    async def no_defer(*args, **kwargs) -> None:
        return None

    async def fake_resolve(name, fetcher, *, url=None, vendors=(), guess=True):
        seen["resolve"].append({"name": name, "url": url, "guess": guess})
        return name, "no board found on any supported ATS"

    monkeypatch.setattr(job, "build_fetcher", fake_fetcher)
    monkeypatch.setattr(job, "defer_if_host_busy", no_defer)
    monkeypatch.setattr(job, "resolve_one", fake_resolve)
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
        name="Acme", ats_type="ashby", slug="acme", source_status=SourceStatus.VERIFIED.value
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
        if len(asked) == 1:
            return None
        return await real(session, ats, slug, other_than=other_than)

    monkeypatch.setattr(job, "holder_of", nobody_at_first)

    await job.handle_discover_company(db_session, _claim(company))

    assert len(asked) == 2
    assert company.source_status == SourceStatus.FAILED.value
    assert company.source_evidence["duplicate_of"] == str(holder.id)
    assert await _crawls(db_session) == 0
    assert holder.source_status == SourceStatus.VERIFIED.value
