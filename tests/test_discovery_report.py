"""`make discovery-report`: every company discovery could not verify, and its fix."""

from __future__ import annotations

import csv
import sys
from datetime import UTC, datetime, timedelta

from packages.core.config import get_settings
from packages.core.enums import SourceStatus
from packages.core.models import Company, CompanyCrawlState
from packages.crawler import discovery_report as report

NOW = datetime.now(UTC)


# --- reading a category --------------------------------------------------------------


def test_a_recorded_category_is_taken_as_recorded() -> None:
    assert report.category_of({"category": "other_job_system"}, "whatever") == "other_job_system"


def test_an_old_reason_is_read_into_a_category() -> None:
    assert report.category_of({}, "blocked: https://a.com/ disallowed by robots.txt") == "blocked"
    assert (
        report.category_of(
            {}, "blocked: https://a.com: robots.txt could not be read; refusing to assume..."
        )
        == "unreachable"
    ), "a robots.txt nobody could fetch is a site that was not reached, not a refusal"
    assert (
        report.category_of(None, "its own site could not be fetched (HTTP 403); name-...")
        == "unreachable"
    )
    assert report.category_of({}, "board found (lever/acme) but it lists no open roles") == (
        "board_empty"
    )


def test_an_old_failure_with_nothing_to_read_waits_for_its_recheck() -> None:
    """Before this, a site with no supported board said only that. Whether it
    has a careers page is not known until discovery tries it again."""
    reason = "its own site names no supported board; name-guessing is off for a company..."

    assert report.category_of({"method": "discovery_failed"}, reason) == "not_yet_rechecked"


def test_a_portfolio_board_has_its_own_fix() -> None:
    assert "several companies" in report.fix_for("multi_company_board")


def test_the_fix_names_the_job_system() -> None:
    assert report.fix_for("other_job_system", "Workday") == "add a reader for Workday"
    assert report.fix_for("no_such_category") == report.FIXES["not_yet_rechecked"]


# --- the companies -------------------------------------------------------------------


async def _failed(session, name: str, *, evidence: dict, reason: str, attempts: int = 1):
    company = Company(
        name=name,
        supplied_website=f"https://{name.lower()}.com",
        source_status=SourceStatus.FAILED.value,
        source_evidence=evidence,
        discovery_failure=reason,
    )
    session.add(company)
    await session.flush()
    session.add(
        CompanyCrawlState(
            company_id=company.id,
            discovery_attempts=attempts,
            discovery_next_at=NOW + timedelta(hours=6),
        )
    )
    await session.flush()
    return company


async def _world(session) -> None:
    await _failed(
        session,
        "Workco",
        evidence={
            "category": "other_job_system",
            "careers_page": "https://workco.com/careers",
            "job_system": "Workday",
            "job_system_url": "https://workco.wd5.myworkdayjobs.com/x",
        },
        reason="careers page https://workco.com/careers uses Workday (...)",
        attempts=2,
    )
    await _failed(
        session,
        "Bamco",
        evidence={
            "category": "other_job_system",
            "careers_page": "https://bamco.com/jobs",
            "job_system": "BambooHR",
        },
        reason="careers page https://bamco.com/jobs uses BambooHR (...)",
    )
    await _failed(
        session,
        "Quietco",
        evidence={"category": "no_careers_page", "tried": ["https://quietco.com/careers"]},
        reason="no careers page found: ...",
    )
    await _failed(
        session,
        "Oldco",
        evidence={"method": "discovery_failed"},
        reason="its own site names no supported board; name-guessing is off ...",
    )
    session.add(Company(name="Verifiedco", source_status=SourceStatus.VERIFIED.value))
    await session.flush()


async def test_every_failed_company_is_listed_careers_pages_first(db_session) -> None:
    await _world(db_session)

    failed = await report.failed_companies(db_session)

    assert [f.name for f in failed] == ["Bamco", "Workco", "Quietco", "Oldco"]
    workco = next(f for f in failed if f.name == "Workco")
    assert (workco.careers_page, workco.job_system, workco.attempts) == (
        "https://workco.com/careers",
        "Workday",
        2,
    )
    assert workco.fix == "add a reader for Workday"
    assert workco.next_attempt is not None


async def test_the_summary_counts_each_fix_and_each_job_system(db_session) -> None:
    await _world(db_session)

    summary = report.summarise(await report.failed_companies(db_session))

    assert summary.total == 4
    assert summary.with_careers_page == 2
    assert dict(summary.by_category) == {
        "other_job_system": 2,
        "no_careers_page": 1,
        "not_yet_rechecked": 1,
    }
    assert dict(summary.by_system) == {"Workday": 1, "BambooHR": 1}


async def test_the_command_writes_one_row_per_failed_company(
    committing_sessionmaker, worker_session, monkeypatch, tmp_path, capsys
) -> None:
    from scripts import discovery_report

    await _world(worker_session)
    await worker_session.commit()
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(discovery_report, "get_sessionmaker", lambda: committing_sessionmaker)
    monkeypatch.setattr(sys, "argv", ["discovery_report"])

    assert await discovery_report.main() == 0

    printed = capsys.readouterr().out
    assert "4 companies not verified; 2 have a careers page on record" in printed
    assert "Workday" in printed and "BambooHR" in printed
    rows = list(csv.DictReader((tmp_path / "exports" / "discovery_failures.csv").open()))
    assert [row["company"] for row in rows] == ["Bamco", "Workco", "Quietco", "Oldco"]
    assert rows[1]["fix"] == "add a reader for Workday"
    assert tuple(rows[0]) == report.COLUMNS
