"""One crawl cycle: poll each company, emit only what changed.

Change detection is the whole point. Career boards are mostly static between
polls, so a cycle that re-emitted every posting each run would flood matching
and the review queue with work that is not new. Two hashes guard against that:

- **Board-level** — if the whole response is byte-identical to last time,
  nothing on that board changed and no postings are parsed at all.
- **Posting-level** — a board that changed usually changed in one posting, so
  each is hashed separately and only genuinely new or edited ones are emitted.

Gate 5 asks that a second run emits zero postings. That falls out of this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Company, Posting
from packages.crawler.extract import CompanySeed, ExtractedPosting, extractor_for
from packages.crawler.fetch import Blocked, PoliteFetcher

log = structlog.get_logger(__name__)


@dataclass
class CompanyResult:
    company: str
    fetched: bool = False
    new_postings: int = 0
    updated_postings: int = 0
    closed_postings: int = 0
    skipped_reason: str | None = None
    error: str | None = None
    waited_seconds: float = 0.0
    #: Set when the board fetched and parsed to nothing while we still hold
    #: open postings for it. Almost always a broken extractor, not an empty
    #: board, so nothing is closed and this is surfaced instead.
    suspect_parse: bool = False

    @property
    def emitted(self) -> int:
        return self.new_postings + self.updated_postings


@dataclass
class CrawlReport:
    results: list[CompanyResult] = field(default_factory=list)

    @property
    def emitted(self) -> int:
        return sum(r.emitted for r in self.results)

    @property
    def fetched(self) -> int:
        return sum(1 for r in self.results if r.fetched)

    @property
    def blocked(self) -> list[str]:
        return [r.company for r in self.results if r.skipped_reason]

    @property
    def failed(self) -> list[str]:
        return [r.company for r in self.results if r.error]

    @property
    def suspect(self) -> list[str]:
        """Boards that parsed to nothing while holding open postings."""
        return [r.company for r in self.results if r.suspect_parse]

    def summary(self) -> str:
        summary = (
            f"{self.fetched} boards fetched, {self.emitted} postings emitted, "
            f"{len(self.blocked)} skipped, {len(self.failed)} failed"
        )
        if self.suspect:
            summary += f", {len(self.suspect)} suspect ({', '.join(self.suspect)})"
        failures = [f"{result.company}: {result.error}" for result in self.results if result.error]
        return f"{summary} [{'; '.join(failures)}]" if failures else summary


async def upsert_company(session: AsyncSession, seed: CompanySeed) -> Company:
    """Find or create the Company row for a seed entry."""
    company = await session.scalar(select(Company).where(Company.name == seed.name))
    if company is None:
        company = Company(name=seed.name)
        session.add(company)

    company.domain = seed.domain
    company.careers_url = seed.careers_url
    company.ats_type = seed.ats
    company.poll_interval_s = seed.poll_interval_s
    await session.flush()
    return company


def is_due(company: Company, *, now: datetime | None = None) -> bool:
    """Whether this company's poll interval has elapsed."""
    if company.last_polled_at is None:
        return True
    current = now or datetime.now(UTC)
    last = company.last_polled_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (current - last).total_seconds() >= company.poll_interval_s


async def _store(
    session: AsyncSession, company: Company, extracted: list[ExtractedPosting]
) -> tuple[int, int]:
    """Upsert postings, returning (new, updated)."""
    existing = {
        posting.external_id: posting
        for posting in (
            await session.scalars(select(Posting).where(Posting.company_id == company.id))
        ).all()
        if posting.external_id
    }

    new_count = 0
    updated_count = 0

    for item in extracted:
        current = existing.get(item.external_id)

        if current is None:
            session.add(
                Posting(
                    company_id=company.id,
                    ats_type=item.ats_type,
                    external_id=item.external_id,
                    url=item.url,
                    title=item.title,
                    location=item.location,
                    description_raw=item.description_raw,
                    published_at=item.published_at,
                    content_hash=item.content_hash,
                )
            )
            new_count += 1
            continue

        if current.content_hash == item.content_hash:
            # Unchanged since the last poll — emit nothing.
            continue

        current.url = item.url
        current.title = item.title
        if item.published_at is not None:
            current.published_at = item.published_at
        current.location = item.location
        current.description_raw = item.description_raw
        current.content_hash = item.content_hash
        # An edited posting is open again even if we had closed it.
        current.closed_at = None
        updated_count += 1

    await session.flush()
    return new_count, updated_count


async def _close_missing(
    session: AsyncSession, company: Company, extracted: list[ExtractedPosting]
) -> tuple[int, bool]:
    """Mark postings the board no longer lists as closed.

    Returns `(closed, suspect)`. A board that fetched cleanly and parsed to
    *nothing* while we still hold open postings is refused: the far likelier
    explanation is that the extractor broke — the ATS changed its payload
    shape, or served a degraded response with a 200 — than that an employer
    closed every requisition between two polls.

    Getting this wrong is expensive and silent. Closing the whole set drops
    those postings out of the match feed with no error anywhere, and the next
    successful crawl re-creates them as new, so even the audit trail reads
    like normal churn. Declining to act costs one stale posting until the
    extractor is fixed; the alternative costs the feed.
    """
    open_postings = (
        await session.scalars(
            select(Posting).where(Posting.company_id == company.id, Posting.closed_at.is_(None))
        )
    ).all()

    if not extracted and open_postings:
        log.warning(
            "crawl_parse_yielded_nothing",
            company=company.name,
            open_postings=len(open_postings),
            action="left open; extractor is the likely fault",
        )
        return 0, True

    seen = {item.external_id for item in extracted}
    closed = 0
    for posting in open_postings:
        if posting.external_id and posting.external_id not in seen:
            posting.closed_at = datetime.now(UTC)
            closed += 1

    await session.flush()
    return closed, False


async def crawl_company(
    session: AsyncSession,
    seed: CompanySeed,
    fetcher: PoliteFetcher,
    *,
    force: bool = False,
) -> CompanyResult:
    """Poll one company. Does not commit."""
    result = CompanyResult(company=seed.name)

    extractor = extractor_for(seed.ats)
    if extractor is None:
        result.skipped_reason = f"no extractor for {seed.ats}"
        return result

    company = await upsert_company(session, seed)

    if not force and not is_due(company):
        result.skipped_reason = "not due yet"
        return result

    url = extractor.board_url(seed.slug)

    try:
        response = await fetcher.fetch(url)
    except Blocked as exc:
        # Being told no is a normal outcome, not an error to work around.
        log.info("crawl_blocked", company=seed.name, reason=str(exc))
        result.skipped_reason = str(exc)
        return result
    except Exception as exc:  # noqa: BLE001 - one bad host must not stop the cycle
        log.warning("crawl_fetch_failed", company=seed.name, error=type(exc).__name__)
        result.error = f"fetch failed: {type(exc).__name__}"
        return result

    result.fetched = True
    result.waited_seconds = response.waited
    company.last_polled_at = datetime.now(UTC)

    if not response.ok:
        result.error = f"HTTP {response.status}"
        log.warning(
            "crawl_board_request_failed",
            company=seed.name,
            slug=seed.slug,
            url=url,
            status=response.status,
        )
        return result

    # Board-level short circuit: identical bytes means nothing to parse.
    if company.board_hash == response.content_hash:
        log.debug("board_unchanged", company=seed.name)
        return result

    extracted = extractor.parse(response.text, seed.slug)
    result.new_postings, result.updated_postings = await _store(session, company, extracted)
    result.closed_postings, result.suspect_parse = await _close_missing(session, company, extracted)

    # A suspect parse deliberately does not record the hash. Recording it
    # would make the next cycle short-circuit on "unchanged" and the warning
    # would never be raised again — the failure would go quiet, which is the
    # thing this is here to prevent.
    if not result.suspect_parse:
        company.board_hash = response.content_hash

    await session.flush()
    return result


async def crawl_all(
    session: AsyncSession,
    seeds: list[CompanySeed],
    fetcher: PoliteFetcher,
    *,
    force: bool = False,
) -> CrawlReport:
    """Run a full cycle over the registry. Does not commit."""
    report = CrawlReport()
    for seed in seeds:
        report.results.append(await crawl_company(session, seed, fetcher, force=force))
    log.info("crawl_cycle_complete", summary=report.summary())
    return report
