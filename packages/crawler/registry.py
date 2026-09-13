"""The one door from a spreadsheet into the database the dispatcher reads.

Before this existed the import path ended at `seeds/companies.yaml` and the
dispatching sweep began at the `companies` table, with nothing carrying one into
the other. Measured on a fresh database: 105 seed entries, 0 `Company` rows,
`tick()` enqueued 0. Only the *inline* crawl path created company rows, so the
dispatcher could not bootstrap itself and the failure was silent — "nothing
due" reads exactly like "nothing to do".

## Which source owns which status

The two stores are not redundant and neither is going away.

- **YAML owns acceptance.** `seeds/companies.yaml` is the curated record of
  boards the owner has accepted, it is git-tracked and reviewable in a diff,
  and its `retired:` section holds dead boards *with the statuses that
  condemned them* (CLAUDE.md §9). `load_seed` reads `companies:` only, so a
  retired board is never polled — and `sync_registry` therefore cannot
  reactivate one, because it never sees it.
- **The database owns discovery and health.** Candidate companies, discovery
  outcomes, verification evidence and crawl state are machine-written, change
  constantly, and number in the thousands. Appending 3,746 unresolved
  candidates to a curated file would destroy the thing that makes it useful.

**Reconciliation.** `sync_registry` writes `verified` only when the row is not
already carrying *newer* evidence, so a board discovery verified this morning is
not overwritten by a seed file stamped last week. Neither direction deletes: a
company in the database and absent from the YAML is a candidate, not a
retirement, and promotion into the YAML stays `import_portals.append_to_registry`'s
job so that file keeps its single writer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.enums import SourceStatus
from packages.core.models import Company, CompanyCrawlState
from packages.crawler.company_csv import Classified, TriageReport
from packages.crawler.extract import CompanySeed

log = structlog.get_logger(__name__)


@dataclass
class RegisterReport:
    created: int = 0
    updated: int = 0
    #: Rows left alone because the stored row already carries better evidence.
    protected: int = 0
    by_status: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        counts = ", ".join(f"{status}={n}" for status, n in sorted(self.by_status.items()))
        return (
            f"{self.created} created, {self.updated} updated, "
            f"{self.protected} left alone ({counts})"
        )


def _domain(url: str) -> str | None:
    try:
        host = (urlparse(url.strip()).hostname or "").lower()
    except ValueError:
        return None
    return host.removeprefix("www.") or None


def status_for(entry: Classified) -> SourceStatus:
    """What an imported row is worth, before anything has been fetched.

    A board-shaped URL is `unverified`, not `verified`: `jobs.lever.co/xchg` is
    the right shape and may still 404, and the whole point of the status column
    is that nothing claims verification without evidence. A website or a careers
    page is a `hint` — a lead for discovery. A row with neither is `no_website`,
    which is a different problem from having tried and failed.
    """
    if entry.promotable:
        return SourceStatus.UNVERIFIED
    if entry.row.website or entry.row.url:
        return SourceStatus.HINT
    return SourceStatus.NO_WEBSITE


async def register_candidates(
    session: AsyncSession, report: TriageReport, *, source_file: str
) -> RegisterReport:
    """Write every imported row into `companies`. Does not commit.

    Idempotent on company **name**, which is the key `crawl.upsert_company`
    already matches on — two keys for one table is how they drift. The limit is
    real and worth stating: two different employers sharing a name collapse into
    one row. The owner's sheet has 3,869 rows and zero duplicate names, and a
    collision is visible in `source_file`/`source_row` rather than silent.

    **A verified row is never downgraded.** Re-importing the same sheet after
    discovery has run must not throw away what discovery learned, so an existing
    `verified` row keeps its status, its ats/slug, its evidence and its
    timestamp; only absent provenance is filled in.
    """
    outcome = RegisterReport()
    now = datetime.now(UTC)
    groups = (
        (report.promotable, "promotable"),
        (report.candidates, "candidate"),
        (report.bespoke, "bespoke"),
        (report.unusable, "unusable"),
    )

    for entries, bucket in groups:
        for entry in entries:
            name = (entry.row.name or "").strip()
            if not name:
                continue
            status = status_for(entry)
            outcome.by_status[status.value] = outcome.by_status.get(status.value, 0) + 1

            company = await session.scalar(select(Company).where(Company.name == name))
            if company is None:
                company = Company(
                    name=name,
                    domain=_domain(entry.row.website or entry.row.url),
                    source_status=status.value,
                    source_evidence={
                        "method": "csv_import",
                        "bucket": bucket,
                        "reason": entry.reason,
                        "at": now.isoformat(),
                    },
                    supplied_website=entry.row.website or None,
                    supplied_career_url=entry.row.career_hint or None,
                    source_file=source_file,
                    source_row=entry.row.source_row or None,
                )
                if entry.promotable:
                    company.ats_type = entry.ats
                    company.slug = entry.slug
                    company.careers_url = entry.row.url
                session.add(company)
                await session.flush()
                outcome.created += 1
            elif company.source_status == SourceStatus.VERIFIED.value:
                # Discovery already proved this board. Keep everything it
                # learned; fill only what the sheet can add and the row lacks.
                company.supplied_website = company.supplied_website or entry.row.website or None
                company.supplied_career_url = (
                    company.supplied_career_url or entry.row.career_hint or None
                )
                company.source_file = company.source_file or source_file
                company.source_row = company.source_row or entry.row.source_row or None
                outcome.protected += 1
            else:
                company.domain = company.domain or _domain(entry.row.website or entry.row.url)
                company.source_status = status.value
                company.supplied_website = entry.row.website or company.supplied_website
                company.supplied_career_url = entry.row.career_hint or company.supplied_career_url
                company.source_file = source_file
                company.source_row = entry.row.source_row or company.source_row
                if entry.promotable:
                    company.ats_type = entry.ats
                    company.slug = entry.slug
                    company.careers_url = entry.row.url
                outcome.updated += 1

            # Discovery is owed for anything not already a verified board.
            # NULL `discovery_next_at` means "as soon as possible", the same
            # convention `next_due_at` uses.
            if company.source_status != SourceStatus.VERIFIED.value:
                await session.execute(
                    pg_insert(CompanyCrawlState)
                    .values(company_id=company.id, discovery_next_at=None)
                    .on_conflict_do_nothing(index_elements=["company_id"])
                )

    await session.flush()
    log.info("companies_registered", source_file=source_file, summary=outcome.summary())
    return outcome


@dataclass
class SyncReport:
    created: int = 0
    verified: int = 0
    #: Seeds skipped because the stored row already holds newer evidence.
    newer_in_db: int = 0

    def summary(self) -> str:
        return (
            f"{self.created} created, {self.verified} marked verified, "
            f"{self.newer_in_db} left alone (newer evidence in the database)"
        )


async def sync_registry(
    session: AsyncSession, seeds: list[CompanySeed], *, now: datetime | None = None
) -> SyncReport:
    """Project the curated YAML registry into `companies`. Does not commit.

    Explicit and idempotent, so it can run at worker start and by hand without
    the dispatcher depending on someone having remembered to run an inline
    crawl first.

    Two things it must not do, both of which would lose information:

    - **Reactivate a retired board.** It cannot: `load_seed` reads `companies:`
      only, so a retired entry never reaches this function.
    - **Overwrite newer verification evidence.** A board discovery verified
      today is not demoted by a seed file stamped last week, so a row already
      `verified` with a `source_verified_at` later than the seed's `checked`
      date is left exactly as it is.
    """
    outcome = SyncReport()
    current = now or datetime.now(UTC)

    for seed in seeds:
        company = await session.scalar(select(Company).where(Company.name == seed.name))
        checked = _seed_checked_at(seed)

        if company is None:
            company = Company(
                name=seed.name,
                domain=seed.domain,
                careers_url=seed.careers_url,
                ats_type=seed.ats,
                slug=seed.slug,
                poll_interval_s=seed.poll_interval_s,
                source_status=SourceStatus.VERIFIED.value,
                source_verified_at=checked or current,
                source_evidence={
                    "method": "registry_sync",
                    "seed_checked": seed.checked,
                    "seed_state": seed.state,
                    "note": "accepted board from the curated registry",
                },
            )
            session.add(company)
            await session.flush()
            outcome.created += 1
            outcome.verified += 1
            continue

        stored = company.source_verified_at
        if (
            company.source_status == SourceStatus.VERIFIED.value
            and stored is not None
            and checked is not None
            and stored > checked
        ):
            outcome.newer_in_db += 1
            continue

        company.ats_type = seed.ats
        company.slug = seed.slug
        company.careers_url = seed.careers_url or company.careers_url
        company.domain = seed.domain or company.domain
        company.poll_interval_s = seed.poll_interval_s
        if company.source_status != SourceStatus.VERIFIED.value:
            company.source_status = SourceStatus.VERIFIED.value
            company.source_verified_at = checked or current
            company.source_evidence = {
                "method": "registry_sync",
                "seed_checked": seed.checked,
                "seed_state": seed.state,
                "note": "accepted board from the curated registry",
            }
            outcome.verified += 1

    await session.flush()
    log.info("registry_synced", summary=outcome.summary())
    return outcome


def _seed_checked_at(seed: CompanySeed) -> datetime | None:
    """The seed's `checked` date as an instant, or None if it never was."""
    if not seed.checked:
        return None
    try:
        parsed = datetime.fromisoformat(str(seed.checked))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


__all__ = [
    "RegisterReport",
    "SyncReport",
    "register_candidates",
    "status_for",
    "sync_registry",
]
