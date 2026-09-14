"""Whether the `companies` table agrees with the curated registry. Read-only.

The dashboard reported zero boards found and 119 companies needing a URL
while 11,851 open postings sat in the database. Nothing was wrong with
extraction: the rows predated `Company.slug`, the source-status migration
could only mark them `no_website`, and `make registry-sync` — which repairs
exactly that — had never been run against the database. The symptom looked
like a crawler failure and the fix was one command nobody was told about.

This names the disagreement and the command, and changes nothing. Applying
the repair stays a deliberate act (`make registry-sync`, or the setup page),
because it makes boards fetchable and the next crawl tick will poll them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.enums import SourceStatus
from packages.core.models import Company
from packages.crawler.extract import CompanySeed, RetiredSeed
from packages.crawler.registry import RETIRED_METHOD

#: How many names to show per problem. Enough to recognize, not a dump.
EXAMPLES = 5

SYNC_FIX = (
    "make registry-sync dry=1   # preview, writes nothing\nmake registry-sync         # apply"
)


@dataclass(frozen=True)
class RegistryProblem:
    code: str
    count: int
    detail: str
    fix: str
    examples: tuple[str, ...] = ()


@dataclass
class RegistryHealth:
    live_seeds: int
    retired_seeds: int
    rows: int
    fetchable: int
    problems: list[RegistryProblem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        head = (
            f"{self.live_seeds} live and {self.retired_seeds} retired registry entries; "
            f"{self.rows} company rows, {self.fetchable} fetchable"
        )
        if self.ok:
            return head + " — in agreement"
        return head + " — " + "; ".join(f"{p.count} {p.detail}" for p in self.problems)


def _is_fetchable(company: Company) -> bool:
    """Mirrors `runs.fetchable()`, which is a SQL expression and cannot be called on a row."""
    return (
        company.source_status == SourceStatus.VERIFIED.value
        and company.slug is not None
        and company.ats_type is not None
    )


def _newer_than(company: Company, checked: str | None) -> bool:
    from packages.crawler.registry import _checked_at

    stamp = _checked_at(checked)
    return (
        company.source_status == SourceStatus.VERIFIED.value
        and company.source_verified_at is not None
        and stamp is not None
        and company.source_verified_at > stamp
    )


def _problem(code: str, names: list[str], detail: str, fix: str) -> RegistryProblem:
    return RegistryProblem(
        code=code,
        count=len(names),
        detail=detail,
        fix=fix,
        examples=tuple(sorted(names)[:EXAMPLES]),
    )


async def diagnose_registry(
    session: AsyncSession, seeds: list[CompanySeed], retired: list[RetiredSeed]
) -> RegistryHealth:
    """Compare the registry with the table, and say what `sync_registry` would repair."""
    rows = list((await session.scalars(select(Company))).all())
    by_name = {company.name: company for company in rows}
    live_names = {seed.name for seed in seeds}

    missing = [seed.name for seed in seeds if seed.name not in by_name]
    stale = [
        seed.name
        for seed in seeds
        if (company := by_name.get(seed.name)) is not None
        and not _newer_than(company, seed.checked)
        and (
            not _is_fetchable(company) or (company.ats_type, company.slug) != (seed.ats, seed.slug)
        )
    ]
    # Named entries only, as (name, entry) so the name is known to be a str.
    retired_only = [
        (entry.name, entry) for entry in retired if entry.name and entry.name not in live_names
    ]
    still_fetchable = [
        name
        for name, entry in retired_only
        if (company := by_name.get(name)) is not None
        and _is_fetchable(company)
        and not _newer_than(company, entry.checked)
    ]
    unmarked = [
        name
        for name, _entry in retired_only
        if (company := by_name.get(name)) is not None
        and not _is_fetchable(company)
        and (company.source_evidence or {}).get("method") != RETIRED_METHOD
    ]
    nameless = [entry.slug for entry in retired if not entry.name]

    problems: list[RegistryProblem] = []
    if missing:
        problems.append(
            _problem(
                "registry_rows_missing",
                missing,
                "registry boards have no company row, so the dispatcher never sees them",
                SYNC_FIX,
            )
        )
    if stale:
        problems.append(
            _problem(
                "registry_rows_stale",
                stale,
                "company rows disagree with their registry entry (not fetchable, or a "
                "different board) — they show as needing a URL although the board is known",
                SYNC_FIX,
            )
        )
    if still_fetchable:
        problems.append(
            _problem(
                "retired_board_still_fetchable",
                still_fetchable,
                "retired boards are still fetchable and will be polled",
                SYNC_FIX,
            )
        )
    if unmarked:
        problems.append(
            _problem(
                "retired_board_unmarked",
                unmarked,
                "rows for retired boards are not marked retired and read as needing a URL",
                SYNC_FIX,
            )
        )
    if nameless:
        problems.append(
            _problem(
                "retired_entry_nameless",
                nameless,
                "retired entries have no name and cannot be matched to a company row",
                "add `name:` to those entries under `retired:` in seeds/companies.yaml",
            )
        )

    return RegistryHealth(
        live_seeds=len(seeds),
        retired_seeds=len(retired),
        rows=len(rows),
        fetchable=sum(1 for company in rows if _is_fetchable(company)),
        problems=problems,
    )


__all__ = ["RegistryHealth", "RegistryProblem", "diagnose_registry"]
