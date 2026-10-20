"""Look harder at one noted company, as its own queue task.

Claimed only when nothing else is runnable, discovery included
(`run.YIELDING_KINDS`): the owner asked for the noted companies to be looked
into once everything else is done. A board found is verified exactly as
discovery verifies one (`verify_board`). Otherwise the note becomes an error
(`needs_owner`) carrying what was tried, what is wrong and what to do.

Idempotent: a note already looked into, or closed by an ordinary run since it
was enqueued, is returned from without a request.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from apps.worker.discover_company_job import lead_for, verify_board
from packages.core.enums import SourceStatus
from packages.core.models import Company
from packages.core.models_issues import CompanyIssue
from packages.core.queue import ClaimedTask
from packages.crawler import issues
from packages.crawler.deep_dive import DeepDive, deep_dive
from packages.crawler.defer import defer_if_host_busy
from packages.crawler.fetch import build_fetcher
from packages.crawler.runs import ensure_state

log = structlog.get_logger(__name__)

DEEP_DIVE_TASK_KIND = issues.DEEP_DIVE_TASK_KIND

__all__ = ["DEEP_DIVE_TASK_KIND", "handle_deep_dive_company"]


class MalformedDeepDiveTask(ValueError):
    """The payload does not name a note to look into."""


def _issue_id(payload: dict[str, Any]) -> uuid.UUID:
    try:
        return uuid.UUID(str(payload["issue_id"]))
    except (KeyError, ValueError) as exc:
        raise MalformedDeepDiveTask(f"deep dive task without an issue: {payload!r}") from exc


async def _apply_found_board(
    session: AsyncSession, company: Company, issue: CompanyIssue, found: DeepDive, now: datetime
) -> bool:
    """Verify the board the deep dive found. False if another company holds it."""
    assert found.board is not None
    state = await ensure_state(session, company.id)
    evidence = {
        "method": "deep_dive",
        "stage": issue.stage,
        "noted_as": issue.category,
        "diagnosis": found.diagnosis,
        "ats": found.board.ats,
        "slug": found.board.slug,
        "board_url": found.board.board_url,
        "open_jobs": found.board.open_jobs,
        "previous_board": (
            f"{company.ats_type}/{company.slug}" if issue.stage == "crawl" else None
        ),
        "at": now.isoformat(),
    }
    return await verify_board(
        session,
        company,
        state,
        found.board,
        evidence=evidence,
        via="deep_dive",
        now=now,
        by="deep_dive",
    )


async def handle_deep_dive_company(session: AsyncSession, claimed: ClaimedTask) -> None:
    """Look harder at one noted company, and fix it or put it on the error list."""
    issue = await session.get(CompanyIssue, _issue_id(claimed.task.payload_json or {}))
    if issue is None or issue.status not in ("noted", "queued"):
        return
    company = await session.get(Company, issue.company_id)
    if company is None:
        return
    now = datetime.now(UTC)
    if issue.stage == "discovery" and company.source_status == SourceStatus.VERIFIED.value:
        # Verified some other way since it was noted.
        await issues.resolve(session, company.id, stage="discovery", by="discovery", now=now)
        return

    lead = lead_for(company)
    evidence = company.source_evidence or {}
    board = (company.ats_type, company.slug) if company.ats_type and company.slug else None
    async with build_fetcher(shared=True) as fetcher:
        if lead:
            # Hands the task back rather than sleeping in the slot when the
            # company's host is busy; the note stays enqueued.
            await defer_if_host_busy(session, claimed, fetcher, lead)
        found = await deep_dive(
            name=company.name,
            stage=issue.stage,
            category=issue.category,
            reason=issue.reason,
            lead=lead,
            careers_page=evidence.get("careers_page"),
            board=board,
            fetcher=fetcher,
        )

    issue.deep_dived_at = now
    issue.findings = {"kind": found.kind, **found.findings}
    issue.diagnosis = found.diagnosis
    issue.fix = found.fix or None
    if found.board is not None:
        verified = await _apply_found_board(session, company, issue, found, now)
        if not verified:
            issue.diagnosis = f"{found.diagnosis}; that board is polled under another company"
        issue.status = "fixed"
        issue.resolved_at = now
        issue.resolved_by = "deep_dive"
    else:
        issue.status = "needs_owner"
    await session.flush()
    log.info(
        "deep_dive_done",
        company=company.name,
        stage=issue.stage,
        category=issue.category,
        status=issue.status,
        diagnosis=issue.diagnosis,
    )
