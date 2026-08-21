"""Discovery: ingest aggregator postings, and grow the registry from them.

Two jobs, and the second is the one that compounds.

**Ingest** stores what the aggregators returned, resolving each posting to a
real ATS form where it can. That gets postings from companies nobody listed
into the match feed today.

**Promotion** is the part that lasts. When a posting resolves to a Greenhouse,
Lever or Ashby board, that board's slug is a company the *crawler* can poll
directly from then on — first-hand, complete, and on our own schedule rather
than whenever an aggregator notices. So a resolved posting is not just a job;
it is a candidate registry entry, and promotion writes it to
`seeds/companies.yaml`.

The registry stops being 29 hand-picked companies and becomes 29 plus
everything discovery has found since. That is the actual answer to "find in
all companies": not one enormous list written up front, but a list that grows
itself from what the aggregators surface.

Promotion never removes an entry and never rewrites one the owner wrote. It
appends, and `validate.py` is what reports the dead ones — deleting somebody's
curated row because a fetch failed once is not a decision this module gets to
make.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Company, Posting
from packages.crawler.aggregators import SOURCES, AggregatorSource, SourceResult, fetch_all
from packages.crawler.extract import CompanySeed, load_seed
from packages.crawler.fetch import PoliteFetcher
from packages.crawler.liveness import check
from packages.crawler.resolve import Resolution, resolve

log = structlog.get_logger(__name__)

#: The slug sits in the same position in all three board URLs.
_SLUG_RES = (
    re.compile(r"^https?://(?:boards|job-boards)\.greenhouse\.io/(?P<slug>[\w.-]+)/", re.I),
    re.compile(r"^https?://jobs\.lever\.co/(?P<slug>[\w.-]+)/", re.I),
    re.compile(r"^https?://jobs\.ashbyhq\.com/(?P<slug>[\w.-]+)/", re.I),
)


@dataclass
class DiscoveryReport:
    sources: list[SourceResult] = field(default_factory=list)
    new_postings: int = 0
    resolved: int = 0
    promoted: list[CompanySeed] = field(default_factory=list)
    #: Aggregator postings re-checked, and how many turned out to be gone.
    verified: int = 0
    closed_stale: int = 0

    @property
    def seen(self) -> int:
        return sum(len(r.postings) for r in self.sources)

    @property
    def failed_sources(self) -> list[str]:
        return [r.source for r in self.sources if not r.ok]

    def summary(self) -> str:
        text = (
            f"{self.seen} postings from {len(self.sources) - len(self.failed_sources)}"
            f"/{len(self.sources)} sources, {self.new_postings} new, "
            f"{self.resolved} resolved to an ATS, {len(self.promoted)} companies promoted"
        )
        if self.verified:
            text += f", {self.verified} re-checked ({self.closed_stale} closed)"
        failures = [f"{r.source}: {r.failure}" for r in self.sources if not r.ok]
        return f"{text} [{'; '.join(failures)}]" if failures else text


def slug_from_ats_url(url: str) -> str | None:
    """The company slug in a supported board URL, or None."""
    for pattern in _SLUG_RES:
        match = pattern.match(url)
        if match:
            return match.group("slug")
    return None


async def _company_for(session: AsyncSession, name: str) -> Company:
    company = await session.scalar(select(Company).where(Company.name == name))
    if company is None:
        company = Company(name=name)
        session.add(company)
        await session.flush()
    return company


async def ingest(
    session: AsyncSession,
    fetcher: PoliteFetcher,
    *,
    sources: tuple[AggregatorSource, ...] = SOURCES,
    limit: int = 500,
    resolve_ats: bool = True,
) -> DiscoveryReport:
    """Fetch every aggregator, store what is new, resolve where it applies."""
    report = DiscoveryReport(sources=await fetch_all(fetcher, sources, limit=limit))

    for source_result in report.sources:
        for item in source_result.postings:
            existing = await session.scalar(
                select(Posting).where(Posting.external_id == item.posting.external_id)
            )
            if existing is not None:
                continue

            company = await _company_for(session, item.company_name)

            resolution = Resolution(url=item.posting.url)
            if resolve_ats:
                resolution = await resolve(item.posting.url, fetcher, company_url=item.company_url)

            session.add(
                Posting(
                    company_id=company.id,
                    # Unresolved stays "unknown" — a lead, not a lie about
                    # what the apply pipeline can finish.
                    ats_type=resolution.ats or "unknown",
                    external_id=item.posting.external_id,
                    url=resolution.url,
                    title=item.posting.title,
                    location=item.posting.location,
                    description_raw=item.posting.description_raw,
                    content_hash=item.posting.content_hash,
                )
            )
            report.new_postings += 1

            if not resolution.applyable:
                continue
            report.resolved += 1

            slug = slug_from_ats_url(resolution.url)
            if slug and company.ats_type is None:
                # The company is now pollable first-hand. This is what makes
                # discovery compound instead of repeating itself.
                company.ats_type = resolution.ats
                company.careers_url = resolution.url

    await session.flush()
    log.info("discovery_done", summary=report.summary())
    return report


async def promote(
    session: AsyncSession, *, seed_path: str | None = None, write: bool = True
) -> list[CompanySeed]:
    """Append newly-pollable companies to the seed registry.

    Only companies discovery resolved to a supported board, and only ones the
    registry does not already carry. Existing entries are never touched.
    """
    existing = load_seed(seed_path)
    known = {(seed.ats, seed.slug) for seed in existing}
    known_names = {seed.name.lower() for seed in existing}

    candidates = (
        await session.scalars(
            select(Company).where(Company.ats_type.isnot(None), Company.careers_url.isnot(None))
        )
    ).all()

    additions: list[CompanySeed] = []
    for company in candidates:
        slug = slug_from_ats_url(company.careers_url or "")
        if slug is None or company.ats_type is None:
            continue
        if (company.ats_type, slug) in known or company.name.lower() in known_names:
            continue
        additions.append(CompanySeed(name=company.name, slug=slug, ats=company.ats_type))
        known.add((company.ats_type, slug))
        known_names.add(company.name.lower())

    if additions and write:
        _append_seeds(additions, seed_path)

    log.info("registry_promoted", added=len(additions))
    return additions


def _append_seeds(additions: list[CompanySeed], seed_path: str | None) -> None:
    """Rewrite the seed file with the additions appended.

    Read-modify-write of the whole document, because the file is a list and
    appending text to YAML by hand is how indentation bugs get in.
    """
    from packages.crawler.extract import default_seed_path

    path = Path(seed_path) if seed_path else default_seed_path()
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    rows = document.get("companies") if isinstance(document, dict) else document
    if not isinstance(rows, list):
        log.warning("seed_file_unexpected_shape", path=str(path))
        return

    rows.extend({"name": seed.name, "slug": seed.slug, "ats": seed.ats} for seed in additions)

    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")


async def verify_open(
    session: AsyncSession,
    fetcher: PoliteFetcher,
    *,
    limit: int = 50,
    older_than: timedelta = timedelta(days=7),
) -> tuple[int, int]:
    """Re-check aggregator postings, closing the ones that are gone.

    Returns `(checked, closed)`.

    Registry postings do not need this — the crawler re-reads their whole
    board every cycle, so a vanished posting is detected by absence. An
    aggregator posting has no board behind it, so without this it stays in the
    match feed forever regardless of whether the job still exists.

    Only `UNKNOWN` is ever ignored: a posting we could not fetch is not a
    posting that closed. Same rule as `_close_missing` in `crawl.py`, arrived
    at the same expensive way.
    """
    cutoff = datetime.now(UTC) - older_than

    stale = list(
        (
            await session.scalars(
                select(Posting)
                .where(
                    Posting.closed_at.is_(None),
                    # Aggregator postings carry a source-namespaced id; a
                    # board posting's id comes from the ATS.
                    Posting.external_id.contains(":"),
                    Posting.first_seen_at < cutoff,
                )
                .order_by(Posting.first_seen_at)
                .limit(limit)
            )
        ).all()
    )

    closed = 0
    for posting in stale:
        verdict = await check(posting.url, fetcher)
        if verdict.is_closed:
            posting.closed_at = datetime.now(UTC)
            closed += 1
            log.info("posting_closed_on_recheck", url=posting.url, reason=verdict.reason)

    await session.flush()
    return len(stale), closed
