"""`make company-issues`: the noted list, and the error list the deep dive left."""

from __future__ import annotations

import csv
import sys

from sqlalchemy import select

from packages.core.config import get_settings
from packages.core.models import Company
from packages.core.models_issues import CompanyIssue
from packages.crawler import issue_report as report
from packages.crawler import issues


async def _world(session) -> None:
    """Two waiting for a deep dive, two on the error list, one fixed by it."""
    rows = [
        ("Waitco", "noted", None, None),
        ("Queuedco", "queued", None, None),
        ("Goneco", "needs_owner", "domain_gone", "its domain no longer exists"),
        ("Dayco", "needs_owner", "other_job_system", "its jobs are on Workday"),
        ("Fixedco", "fixed", "other_address", "its site answers at www.fixedco.com"),
    ]
    for name, status, kind, diagnosis in rows:
        company = Company(name=name, supplied_website=f"https://{name.lower()}.com")
        session.add(company)
        await session.flush()
        await issues.note(
            session, company.id, stage="discovery", category="unreachable", reason="why"
        )
        if status == "noted":
            continue
        issue = await session.scalar(
            select(CompanyIssue).where(CompanyIssue.company_id == company.id)
        )
        issue.status = status
        issue.diagnosis = diagnosis
        issue.fix = "a fix" if diagnosis else None
        issue.findings = {"kind": kind} if kind else {}
        issue.resolved_by = "deep_dive" if status == "fixed" else None
    await session.flush()


async def test_the_lists_are_split_by_where_each_company_is(db_session) -> None:
    await _world(db_session)

    lists = await report.read_lists(db_session)

    assert [n.name for n in lists.noted] == ["Queuedco", "Waitco"]
    assert [e.name for e in lists.errors] == ["Goneco", "Dayco"]
    assert lists.fixed_by_deep_dive == 1
    assert dict(lists.errors_by_kind) == {"domain_gone": 1, "other_job_system": 1}


async def test_the_command_writes_both_lists(
    committing_sessionmaker, worker_session, monkeypatch, tmp_path, capsys
) -> None:
    from scripts import company_issues

    await _world(worker_session)
    await worker_session.commit()
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(company_issues, "get_sessionmaker", lambda: committing_sessionmaker)
    monkeypatch.setattr(sys, "argv", ["company_issues"])

    assert await company_issues.main() == 0

    printed = capsys.readouterr().out
    assert "2 noted, waiting for a deep dive" in printed
    assert "2 on the error list" in printed
    assert "1 fixed by the deep dive" in printed
    noted = list(csv.DictReader((tmp_path / "exports" / "companies_noted.csv").open()))
    errors = list(csv.DictReader((tmp_path / "exports" / "company_errors.csv").open()))
    assert [r["company"] for r in noted] == ["Queuedco", "Waitco"]
    assert [(r["company"], r["kind"]) for r in errors] == [
        ("Goneco", "domain_gone"),
        ("Dayco", "other_job_system"),
    ]
    assert errors[0]["diagnosis"] == "its domain no longer exists"
