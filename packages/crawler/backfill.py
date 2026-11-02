"""Read every missing job description now, rather than five a crawl.

Workday, Rippling and Breezy list title, place and link; the text is on each
job's own page, and a crawl reads at most five of those (`details.py`). That
budget keeps one crawl short, and it leaves a board that was just added, or
whose crawls were held back, with hundreds of postings and no text: nothing
for the assistant's search to find and little for scoring to read.

This reads them all, one host at a time per host, through the crawler's
PoliteFetcher: robots.txt is honoured and §2.6's floor holds, because the
floor is per host and every host here waits its own turn. Each page is stored
through the crawler's own `_store`, so a version is written and the vector is
dropped exactly as for any edited posting, and the next matching pass embeds,
scores and chunks it.

Not a crawl. It never reads a list, so it opens nothing and closes nothing; a
page that fails leaves that posting waiting, and whether it closed is for the
next read of the list to say. Every posting is committed as it lands, so a
run that is stopped keeps what it finished, and running it again reads only
what is still missing.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Company, Posting
from packages.crawler import breezy, rippling, workday
from packages.crawler.crawl import _store
from packages.crawler.details import ReadDetail
from packages.crawler.extract import ExtractedPosting
from packages.crawler.fetch import Blocked, PoliteFetcher

log = structlog.get_logger(__name__)

DetailUrlFor = Callable[[str, ExtractedPosting], str]

#: The boards whose lists carry no description, and how to read one job page.
READERS: dict[str, tuple[DetailUrlFor, ReadDetail]] = {
    "workday": (workday.detail_url_for, workday.read_detail),
    "rippling": (rippling.detail_url_for, rippling.read_detail),
    "breezy": (breezy.detail_url_for, breezy.read_detail),
}

#: Hosts read at once. Each waits a minute between its own requests, so this
#: bounds open sessions, not the request rate to any one host.
MAX_HOSTS = 48

Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True)
class Pending:
    """One open posting with no text, and where its page is."""

    company_id: uuid.UUID
    company_slug: str
    url: str
    listing: ExtractedPosting


@dataclass
class HostReport:
    host: str
    pending: int
    filled: int = 0
    failed: int = 0
    blocked: bool = False


@dataclass
class BackfillReport:
    hosts: list[HostReport] = field(default_factory=list)

    @property
    def filled(self) -> int:
        return sum(h.filled for h in self.hosts)

    @property
    def failed(self) -> int:
        return sum(h.failed for h in self.hosts)

    @property
    def blocked_hosts(self) -> list[str]:
        return [h.host for h in self.hosts if h.blocked]


async def find_pending(
    session: AsyncSession, *, ats: Iterable[str] = tuple(READERS)
) -> dict[str, list[Pending]]:
    """Open postings with no description, grouped by the host their pages are on.

    Newest first within a host, so a run that is stopped early has read the
    postings most likely to still be open.
    """
    kinds = [kind for kind in ats if kind in READERS]
    rows = (
        await session.execute(
            select(
                Posting.external_id,
                Posting.url,
                Posting.title,
                Posting.location,
                Posting.published_at,
                Posting.ats_type,
                Company.id.label("company_id"),
                Company.slug,
            )
            .join(Company, Company.id == Posting.company_id)
            .where(
                Posting.closed_at.is_(None),
                Posting.description_raw.is_(None),
                Posting.external_id.is_not(None),
                Posting.ats_type.in_(kinds),
                Company.slug.is_not(None),
            )
            .order_by(Posting.first_seen_at.desc())
        )
    ).all()

    grouped: dict[str, list[Pending]] = {}
    for row in rows:
        listing = ExtractedPosting(
            external_id=row.external_id,
            url=row.url,
            title=row.title,
            location=row.location,
            published_at=row.published_at,
            ats_type=row.ats_type,
        )
        detail_url_for, _ = READERS[row.ats_type]
        try:
            url = detail_url_for(row.slug, listing)
        except (ValueError, RuntimeError) as exc:
            log.warning("backfill_unplaceable", posting=row.external_id, error=type(exc).__name__)
            continue
        host = urlparse(url).hostname or ""
        grouped.setdefault(host, []).append(Pending(row.company_id, row.slug, url, listing))
    return grouped


async def fill_host(
    fetcher: PoliteFetcher,
    session: AsyncSession,
    host: str,
    items: list[Pending],
    *,
    on_progress: Callable[[HostReport], None] | None = None,
) -> HostReport:
    """Read each page in turn; the fetcher spaces them by the host's floor."""
    report = HostReport(host=host, pending=len(items))
    companies: dict[uuid.UUID, Company] = {}
    for item in items:
        filled = await _fill_one(fetcher, session, item, companies)
        if filled is None:
            report.blocked = True
            break
        if filled:
            report.filled += 1
        else:
            report.failed += 1
        if on_progress is not None:
            on_progress(report)
    log.info(
        "backfill_host_done",
        host=host,
        filled=report.filled,
        failed=report.failed,
        blocked=report.blocked,
    )
    return report


async def _fill_one(
    fetcher: PoliteFetcher,
    session: AsyncSession,
    item: Pending,
    companies: dict[uuid.UUID, Company],
) -> bool | None:
    """True when stored, False when this posting is left waiting, None when refused."""
    listing = item.listing
    _, read_detail = READERS[listing.ats_type or ""]
    try:
        response = await fetcher.fetch(item.url)
    except Blocked:
        # robots.txt refusing a job page refuses the host: never routed round.
        log.info("backfill_blocked", host=urlparse(item.url).hostname)
        return None
    except Exception as exc:  # noqa: BLE001 - one page must not stop the host
        log.warning("backfill_fetch_failed", posting=listing.external_id, error=type(exc).__name__)
        return False
    if not response.ok:
        log.info("backfill_page_missing", posting=listing.external_id, status=response.status)
        return False
    try:
        hydrated = read_detail(response.text, listing)
    except Exception as exc:  # noqa: BLE001 - an unreadable page leaves it waiting
        log.warning("backfill_unreadable", posting=listing.external_id, error=type(exc).__name__)
        return False
    if hydrated is None or not hydrated.description_raw:
        return False

    listing.description_raw = hydrated.description_raw
    listing.published_at = hydrated.published_at or listing.published_at
    listing.content_hash = hydrated.content_hash
    try:
        company = companies.get(item.company_id) or await session.get(Company, item.company_id)
        if company is None:
            return False
        companies[item.company_id] = company
        await _store(session, company, [listing], now=datetime.now(UTC))
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - one bad row must not stop the host
        await session.rollback()
        companies.pop(item.company_id, None)
        log.warning("backfill_store_failed", posting=listing.external_id, error=type(exc).__name__)
        return False
    return True


async def backfill(
    fetcher: PoliteFetcher,
    sessions: Sessions,
    *,
    ats: Iterable[str] = tuple(READERS),
    limit: int | None = None,
    max_hosts: int = MAX_HOSTS,
    on_progress: Callable[[HostReport], None] | None = None,
) -> BackfillReport:
    """Fill every missing description on the given boards, hosts side by side.

    The longest hosts start first: at one request a minute each, they decide
    when the run ends.
    """
    async with sessions() as session:
        pending = await find_pending(session, ats=ats)
    semaphore = asyncio.Semaphore(max_hosts)

    async def one(host: str, items: list[Pending]) -> HostReport:
        async with semaphore, sessions() as session:
            return await fill_host(
                fetcher, session, host, items[:limit] if limit else items, on_progress=on_progress
            )

    ordered = sorted(pending.items(), key=lambda pair: -len(pair[1]))
    reports = await asyncio.gather(*(one(host, items) for host, items in ordered))
    return BackfillReport(hosts=list(reports))
