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


def _probe_returning(seen: dict, jobs: int):
    async def fake_probe(fetcher, vendor, slug):
        seen["probes"].append((vendor, slug))
        return jobs, f"https://api.example/{vendor}/{slug}", None

    return fake_probe


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
    monkeypatch.setattr(job, "_probe", _probe_returning(calls, jobs=4))
    company = await _company(db_session, website="https://acme.com")

    await job.handle_discover_company(db_session, _claim(company))

    assert company.source_status == SourceStatus.VERIFIED.value
    assert (company.ats_type, company.slug) == ("ashby", "acme")
    assert company.source_evidence["method"] == "discovery_from_directory"
    assert company.source_evidence["why"] == "slug matches website"
    assert calls["resolve"] == [], "the site and the guesses are not needed"
    assert await _crawls(db_session) == 1, "the board's first fetch is scheduled"


async def test_a_directory_board_with_no_open_jobs_falls_through_to_the_site(
    db_session, calls, monkeypatch
) -> None:
    """A 200 with an empty list is usually a slug nobody owns."""
    monkeypatch.setattr(
        job, "load_directory", lambda: _directory(("ashby", "acme", "Acme Robotics"))
    )
    monkeypatch.setattr(job, "_probe", _probe_returning(calls, jobs=0))
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
