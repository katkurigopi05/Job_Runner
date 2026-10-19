"""Every company discovery could not verify, with what was found and how to fix it.

Asked for by the owner on 2026-10-19: a company whose careers page was not
found should be noted, and fixed where it can be, not left to be retried for
ever. Discovery now records what a company's own site showed on each failure
(`SiteReading.category` and its fields, in `source_evidence`). This turns
those records into one row per company and a count per fix.

A company that failed before that change has no category yet. It is reported
as `not_yet_rechecked` until its next attempt, and its old reason is kept.
Read-only: nothing here changes a company or a schedule.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.enums import SourceStatus
from packages.core.models import Company, CompanyCrawlState

#: The fix each category needs, in the words a person acts on. `{system}` is
#: the job system's name.
FIXES: dict[str, str] = {
    "other_job_system": "add a reader for {system}",
    "structured_jobs": "read its careers page's structured jobs (the JSON-LD reader)",
    "careers_page_unreadable": "look at its careers page; add its job board URL if it has one",
    "no_careers_page": "add a careers URL to the sheet, or it may not be hiring",
    "multi_company_board": "its careers link is a job board for several companies (a "
    "portfolio or aggregator); add its own board URL to the sheet if it has one",
    "no_website": "add a website or careers URL to the sheet",
    "board_empty": "none: its board lists nothing open; retried on schedule",
    "blocked": "none: its site disallows crawling (robots.txt)",
    "unreachable": "none for now: its site could not be reached (often a closed company); "
    "retried on schedule",
    "not_yet_rechecked": "none yet: waits for its next attempt with the new search",
}

#: Reasons written before categories existed, by the words that begin them.
_LEGACY: tuple[tuple[str, str], ...] = (
    ("blocked:", "blocked"),
    ("its own site could not be fetched", "unreachable"),
    ("no website to read", "no_website"),
    ("board found", "board_empty"),
)


def category_of(evidence: dict[str, Any] | None, reason: str | None) -> str:
    """The recorded category, or one read from an old reason, or `not_yet_rechecked`."""
    recorded = (evidence or {}).get("category")
    if recorded in FIXES:
        return str(recorded)
    text = (reason or "").strip()
    if text.startswith("blocked:") and "disallow" not in text.lower():
        # robots.txt could not be read, or too many redirects: the site was
        # not reached, not a refusal (see `SiteReading.category`).
        return "unreachable"
    for prefix, category in _LEGACY:
        if text.startswith(prefix):
            return category
    return "not_yet_rechecked"


def fix_for(category: str, system: str | None = None) -> str:
    return FIXES.get(category, FIXES["not_yet_rechecked"]).format(system=system or "its job system")


@dataclass(frozen=True)
class Failed:
    """One company discovery could not verify."""

    company_id: uuid.UUID
    name: str
    website: str | None
    careers_page: str | None
    category: str
    fix: str
    job_system: str | None
    job_system_url: str | None
    structured_jobs: int
    reason: str
    attempts: int
    next_attempt: datetime | None


async def failed_companies(session: AsyncSession) -> list[Failed]:
    """Every failed company, those with a careers page first, then by name."""
    rows = (
        await session.execute(
            select(Company, CompanyCrawlState)
            .outerjoin(CompanyCrawlState, CompanyCrawlState.company_id == Company.id)
            .where(Company.source_status == SourceStatus.FAILED.value)
        )
    ).all()
    out = []
    for company, state in rows:
        evidence = company.source_evidence or {}
        category = category_of(evidence, company.discovery_failure)
        system = evidence.get("job_system")
        out.append(
            Failed(
                company_id=company.id,
                name=company.name,
                website=company.supplied_website or company.domain,
                careers_page=evidence.get("careers_page"),
                category=category,
                fix=fix_for(category, system),
                job_system=system,
                job_system_url=evidence.get("job_system_url"),
                structured_jobs=int(evidence.get("structured_jobs") or 0),
                reason=company.discovery_failure or "",
                attempts=state.discovery_attempts if state else 0,
                next_attempt=state.discovery_next_at if state else None,
            )
        )
    return sorted(out, key=lambda f: (f.careers_page is None, f.category, f.name.lower()))


@dataclass(frozen=True)
class Summary:
    total: int
    by_category: list[tuple[str, int]]
    by_system: list[tuple[str, int]]
    with_careers_page: int


def summarise(failed: Sequence[Failed]) -> Summary:
    categories = Counter(f.category for f in failed)
    systems = Counter(f.job_system for f in failed if f.job_system)
    return Summary(
        total=len(failed),
        by_category=categories.most_common(),
        by_system=systems.most_common(),
        with_careers_page=sum(1 for f in failed if f.careers_page),
    )


COLUMNS = (
    "company",
    "website",
    "careers_page",
    "category",
    "fix",
    "job_system",
    "job_system_url",
    "structured_jobs",
    "reason",
    "attempts",
    "next_attempt",
)


def as_row(failed: Failed) -> dict[str, str]:
    return {
        "company": failed.name,
        "website": failed.website or "",
        "careers_page": failed.careers_page or "",
        "category": failed.category,
        "fix": failed.fix,
        "job_system": failed.job_system or "",
        "job_system_url": failed.job_system_url or "",
        "structured_jobs": str(failed.structured_jobs or ""),
        "reason": failed.reason,
        "attempts": str(failed.attempts),
        "next_attempt": failed.next_attempt.isoformat(timespec="minutes")
        if failed.next_attempt
        else "",
    }


__all__ = [
    "COLUMNS",
    "FIXES",
    "Failed",
    "Summary",
    "as_row",
    "category_of",
    "failed_companies",
    "fix_for",
    "summarise",
]
