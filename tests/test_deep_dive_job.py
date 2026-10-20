"""The deep dive task: a board found is verified, anything else goes on the error list."""

from __future__ import annotations

import contextlib

import pytest
from sqlalchemy import func, select

from apps.worker import deep_dive_job as job
from packages.core.enums import SourceStatus
from packages.core.models import Company, QueueTask
from packages.core.models_issues import CompanyIssue
from packages.core.queue import ClaimedTask
from packages.crawler import issues
from packages.crawler.deep_dive import DeepDive
from packages.crawler.dispatch import CRAWL_COMPANY_TASK_KIND
from packages.crawler.find_boards import Resolved


@pytest.fixture
def dives(monkeypatch) -> dict:
    """Fakes the network; `seen["found"]` is what the deep dive returns."""
    seen: dict = {
        "asked": [],
        "found": DeepDive(diagnosis="d", fix="f", findings={"k": 1}, kind="refuses"),
    }

    @contextlib.asynccontextmanager
    async def fake_fetcher(**_kwargs):
        yield object()

    async def no_defer(*args, **kwargs) -> None:
        return None

    async def fake_dive(**kwargs):
        seen["asked"].append(kwargs)
        return seen["found"]

    monkeypatch.setattr(job, "build_fetcher", fake_fetcher)
    monkeypatch.setattr(job, "defer_if_host_busy", no_defer)
    monkeypatch.setattr(job, "deep_dive", fake_dive)
    return seen


def _board(slug: str = "acme") -> Resolved:
    return Resolved(
        name="Acme", ats="greenhouse", slug=slug, board_url=f"https://x/{slug}", open_jobs=4
    )


async def _noted(session, *, stage: str = "discovery", category: str = "unreachable", **company):
    acme = Company(
        name="Acme",
        supplied_website="https://acme.com",
        source_status=SourceStatus.FAILED.value,
        source_evidence={"careers_page": "https://acme.com/careers"},
        **company,
    )
    session.add(acme)
    await session.flush()
    await issues.note(session, acme.id, stage=stage, category=category, reason="why")
    [issue_id] = await issues.start_deep_dives(session, limit=1)
    return acme, await session.get(CompanyIssue, issue_id)


def _claim(issue: CompanyIssue) -> ClaimedTask:
    task = QueueTask(kind=job.DEEP_DIVE_TASK_KIND, payload_json={"issue_id": str(issue.id)})
    return ClaimedTask(task=task, reclaimed=False, previous_owner=None)


async def _crawls(session) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(QueueTask)
            .where(QueueTask.kind == CRAWL_COMPANY_TASK_KIND)
        )
    )


async def _reload(session, issue: CompanyIssue) -> CompanyIssue:
    await session.refresh(issue)
    return issue


async def test_nothing_found_puts_the_company_on_the_error_list(db_session, dives) -> None:
    acme, issue = await _noted(db_session)

    await job.handle_deep_dive_company(db_session, _claim(issue))

    issue = await _reload(db_session, issue)
    assert (issue.status, issue.diagnosis, issue.fix, issue.findings) == (
        "needs_owner",
        "d",
        "f",
        {"kind": "refuses", "k": 1},
    )
    assert issue.deep_dived_at is not None
    [asked] = dives["asked"]
    assert (asked["category"], asked["lead"], asked["careers_page"]) == (
        "unreachable",
        "https://acme.com",
        "https://acme.com/careers",
    )


async def test_a_board_found_is_verified_and_crawled(db_session, dives) -> None:
    dives["found"] = DeepDive(board=_board(), diagnosis="its site answers at www.acme.com")
    acme, issue = await _noted(db_session)

    await job.handle_deep_dive_company(db_session, _claim(issue))

    issue = await _reload(db_session, issue)
    assert (issue.status, issue.resolved_by) == ("fixed", "deep_dive")
    assert (acme.source_status, acme.ats_type, acme.slug) == ("verified", "greenhouse", "acme")
    assert acme.source_evidence["method"] == "deep_dive"
    assert await _crawls(db_session) == 1


async def test_a_board_that_moved_replaces_the_old_one(db_session, dives) -> None:
    dives["found"] = DeepDive(board=_board("acme-new"), diagnosis="moved")
    acme, issue = await _noted(
        db_session, stage="crawl", category="board_gone", ats_type="lever", slug="acme"
    )
    acme.source_status = SourceStatus.VERIFIED.value

    await job.handle_deep_dive_company(db_session, _claim(issue))

    assert dives["asked"][0]["board"] == ("lever", "acme")
    assert (acme.ats_type, acme.slug) == ("greenhouse", "acme-new")
    assert acme.source_evidence["previous_board"] == "lever/acme"
    assert (await _reload(db_session, issue)).status == "fixed"


async def test_a_board_another_company_holds_is_not_taken(db_session, dives) -> None:
    db_session.add(
        Company(
            name="Acme Inc",
            ats_type="greenhouse",
            slug="acme",
            source_status=SourceStatus.VERIFIED.value,
        )
    )
    dives["found"] = DeepDive(board=_board(), diagnosis="found")
    acme, issue = await _noted(db_session)

    await job.handle_deep_dive_company(db_session, _claim(issue))

    issue = await _reload(db_session, issue)
    assert issue.status == "fixed"
    assert issue.diagnosis == "found; that board is polled under another company"
    assert acme.source_status != SourceStatus.VERIFIED.value


async def test_a_note_closed_since_it_was_enqueued_is_left_alone(db_session, dives) -> None:
    acme, issue = await _noted(db_session)
    await issues.resolve(db_session, acme.id, stage="discovery", by="discovery")

    await job.handle_deep_dive_company(db_session, _claim(await _reload(db_session, issue)))

    assert dives["asked"] == []


async def test_a_company_verified_since_it_was_noted_is_closed_not_dived(db_session, dives) -> None:
    acme, issue = await _noted(db_session)
    acme.source_status = SourceStatus.VERIFIED.value

    await job.handle_deep_dive_company(db_session, _claim(issue))

    assert dives["asked"] == []
    assert (await _reload(db_session, issue)).status == "fixed"
