"""The two lists: companies noted and waiting, and the errors the deep dive left.

The owner asked on 2026-10-19 for a worker to note a company it could not
read, move on, look into the noted ones once everything else was done, and
put what it could not fix on a separate list. `company_issues` is where both
lists live (`issues.py`, `deep_dive.py`); this reads them out.

Read-only: nothing here changes a note or a company.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Company
from packages.core.models_issues import CompanyIssue


@dataclass(frozen=True)
class Listed:
    """One company on either list."""

    name: str
    website: str | None
    stage: str
    category: str
    status: str
    kind: str
    reason: str
    diagnosis: str
    fix: str
    times_noted: int
    noted_at: datetime
    deep_dived_at: datetime | None


@dataclass(frozen=True)
class Lists:
    #: Noted and waiting for a deep dive (`noted`, or enqueued as `queued`).
    noted: list[Listed]
    #: Looked into, and not fixed: what to do is on each row.
    errors: list[Listed]
    errors_by_kind: list[tuple[str, int]]
    fixed_by_deep_dive: int
    fixed_by_later_run: int


def _listed(issue: CompanyIssue, company: Company) -> Listed:
    return Listed(
        name=company.name,
        website=company.supplied_website or company.domain,
        stage=issue.stage,
        category=issue.category,
        status=issue.status,
        kind=str((issue.findings or {}).get("kind") or issue.category),
        reason=issue.reason,
        diagnosis=issue.diagnosis or "",
        fix=issue.fix or "",
        times_noted=issue.times_noted,
        noted_at=issue.noted_at,
        deep_dived_at=issue.deep_dived_at,
    )


async def read_lists(session: AsyncSession) -> Lists:
    rows = (
        await session.execute(
            select(CompanyIssue, Company)
            .join(Company, Company.id == CompanyIssue.company_id)
            .where(CompanyIssue.status != "fixed")
        )
    ).all()
    listed = [_listed(issue, company) for issue, company in rows]
    noted = sorted((x for x in listed if x.status != "needs_owner"), key=lambda x: x.name.lower())
    errors = sorted(
        (x for x in listed if x.status == "needs_owner"), key=lambda x: (x.kind, x.name.lower())
    )
    fixed: dict[str | None, int] = {
        by: int(count)
        for by, count in (
            await session.execute(
                select(CompanyIssue.resolved_by, func.count())
                .where(CompanyIssue.status == "fixed")
                .group_by(CompanyIssue.resolved_by)
            )
        ).tuples()
    }
    return Lists(
        noted=noted,
        errors=errors,
        errors_by_kind=Counter(x.kind for x in errors).most_common(),
        fixed_by_deep_dive=fixed.get("deep_dive", 0),
        fixed_by_later_run=sum(count for by, count in fixed.items() if by != "deep_dive"),
    )


NOTED_COLUMNS = ("company", "website", "stage", "category", "reason", "times_noted", "noted_at")
ERROR_COLUMNS = (
    "company",
    "website",
    "stage",
    "kind",
    "diagnosis",
    "fix",
    "noted_as",
    "times_noted",
    "noted_at",
    "deep_dived_at",
)


def _when(moment: datetime | None) -> str:
    return moment.isoformat(timespec="minutes") if moment else ""


def noted_row(item: Listed) -> dict[str, str]:
    return {
        "company": item.name,
        "website": item.website or "",
        "stage": item.stage,
        "category": item.category,
        "reason": item.reason,
        "times_noted": str(item.times_noted),
        "noted_at": _when(item.noted_at),
    }


def error_row(item: Listed) -> dict[str, str]:
    return {
        "company": item.name,
        "website": item.website or "",
        "stage": item.stage,
        "kind": item.kind,
        "diagnosis": item.diagnosis,
        "fix": item.fix,
        "noted_as": item.category,
        "times_noted": str(item.times_noted),
        "noted_at": _when(item.noted_at),
        "deep_dived_at": _when(item.deep_dived_at),
    }


__all__ = [
    "ERROR_COLUMNS",
    "NOTED_COLUMNS",
    "Listed",
    "Lists",
    "error_row",
    "noted_row",
    "read_lists",
]
