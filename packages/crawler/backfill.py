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
from collections import deque
from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
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
from packages.crawler.ratelimit import HostRateLimiter, RateLimitTooLow

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

#: Answers that mean the site does not want the request, not that one job is gone.
REFUSALS = frozenset({401, 403, 429})
#: A host that refuses this many pages in a row, having served none, is left
#: alone (§2.5): asking again a minute later for each of its postings would be
#: pressing past a no.
REFUSALS_BEFORE_STOP = 3
#: The same for a host that has served pages this run. Big Workday tenants
#: refuse about one page in five and serve the rest (2026-11-02), so three in
#: a row came by chance and stopped J.B. Hunt with 498 pages to go. Ten in a
#: row from a host that was serving is the host changing its answer.
REFUSALS_BEFORE_STOP_SERVING = 10

Outcome = Literal["filled", "waiting", "skipped", "refused", "blocked"]

#: §2.6, amended for one backfill at the owner's asking (2026-11-02): job
#: pages at 5 s a host, stepping to 10, 15 and 20 if the sites push back. The
#: crawler's own limiters still refuse anything under 60 s; this floor is the
#: backfill's alone, and only with `--pace`.
MIN_BACKFILL_DELAY_SECONDS = 5.0


@dataclass
class Pace:
    """A delay that only ever slows down, read by every host of one run.

    When `step_up_share` of the last `window` pages, across all hosts, were
    refused, the next step is taken. Refused that much at the slowest step,
    the run stops: the sites are saying no, and §2.5 leaves them alone.
    Tonight's ordinary refusals were 6 to 8 percent; 30 is a pushback.
    """

    steps: tuple[float, ...]
    window: int = 60
    step_up_share: float = 0.3
    stopped: bool = False
    _index: int = 0
    _recent: deque[bool] = field(default_factory=deque)

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("a pace needs at least one step")
        if min(self.steps) < MIN_BACKFILL_DELAY_SECONDS:
            raise RateLimitTooLow(min(self.steps), MIN_BACKFILL_DELAY_SECONDS)
        if list(self.steps) != sorted(self.steps):
            raise ValueError("a pace's steps only get slower")
        self._recent = deque(maxlen=self.window)

    @property
    def current(self) -> float:
        return self.steps[self._index]

    def record(self, *, refused: bool) -> None:
        if self.stopped:
            return
        self._recent.append(refused)
        if len(self._recent) < self.window:
            return
        share = sum(self._recent) / self.window
        if share < self.step_up_share:
            return
        if self._index + 1 < len(self.steps):
            self._index += 1
            self._recent.clear()
            log.warning("backfill_pace_slower", seconds=self.current, refused=round(share, 2))
        else:
            self.stopped = True
            log.warning("backfill_pace_stopped", seconds=self.current, refused=round(share, 2))


@dataclass
class PacedRateLimiter(HostRateLimiter):
    """The in-process limiter, spaced by a `Pace` instead of §2.6's 60 s.

    Everything else is the crawler's: a 429 or Retry-After backs a host off
    for as long as it asks, and a site's own Crawl-delay raises its delay.
    Only the backfill builds one; it is the only process reading these hosts,
    one task per host, so the counters need not be shared.
    """

    pace: Pace = field(default_factory=lambda: Pace(steps=(60.0,)))

    def delay_for(self, host: str) -> float:
        return max(self.pace.current, self.host_delays.get(host, 0.0))


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
    #: Closed, filled or removed by something else since the run began.
    skipped: int = 0
    #: robots.txt refused a job page.
    blocked: bool = False
    #: The site refused `REFUSALS_BEFORE_STOP` pages in a row.
    refused: bool = False
    #: The run's pace stopped: the sites were refusing at its slowest step.
    paced_out: bool = False


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
    def skipped(self) -> int:
        return sum(h.skipped for h in self.hosts)

    @property
    def blocked_hosts(self) -> list[str]:
        return [h.host for h in self.hosts if h.blocked]

    @property
    def refused_hosts(self) -> list[str]:
        return [h.host for h in self.hosts if h.refused]


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
    on_left_waiting: Callable[[str], None] | None = None,
    pace: Pace | None = None,
) -> HostReport:
    """Read each page in turn; the fetcher spaces them by the host's floor."""
    report = HostReport(host=host, pending=len(items))
    companies: dict[uuid.UUID, Company] = {}
    refusals = 0
    for item in items:
        if pace is not None and pace.stopped:
            report.paced_out = True
            break
        outcome = await _fill_one(fetcher, session, item, companies)
        if outcome == "blocked":
            report.blocked = True
            break
        if pace is not None:
            pace.record(refused=outcome == "refused")
        if outcome == "filled":
            report.filled += 1
            refusals = 0
        elif outcome == "skipped":
            report.skipped += 1
            refusals = 0
        else:
            report.failed += 1
            refusals = refusals + 1 if outcome == "refused" else 0
            if on_left_waiting is not None:
                on_left_waiting(item.listing.external_id)
        if on_progress is not None:
            on_progress(report)
        if refusals >= (REFUSALS_BEFORE_STOP_SERVING if report.filled else REFUSALS_BEFORE_STOP):
            report.refused = True
            break
    log.info(
        "backfill_host_done",
        host=host,
        filled=report.filled,
        failed=report.failed,
        blocked=report.blocked,
        refused=report.refused,
    )
    return report


async def _fill_one(
    fetcher: PoliteFetcher,
    session: AsyncSession,
    item: Pending,
    companies: dict[uuid.UUID, Company],
) -> Outcome:
    """Read and store one page; anything short of stored leaves the posting waiting."""
    listing = item.listing
    _, read_detail = READERS[listing.ats_type or ""]
    try:
        response = await fetcher.fetch(item.url)
    except Blocked:
        # robots.txt refusing a job page refuses the host: never routed round.
        log.info("backfill_blocked", host=urlparse(item.url).hostname)
        return "blocked"
    except Exception as exc:  # noqa: BLE001 - one page must not stop the host
        log.warning("backfill_fetch_failed", posting=listing.external_id, error=type(exc).__name__)
        return "waiting"
    if not response.ok:
        log.info("backfill_page_missing", posting=listing.external_id, status=response.status)
        return "refused" if response.status in REFUSALS else "waiting"
    try:
        hydrated = read_detail(response.text, listing)
    except Exception as exc:  # noqa: BLE001 - an unreadable page leaves it waiting
        log.warning("backfill_unreadable", posting=listing.external_id, error=type(exc).__name__)
        return "waiting"
    if hydrated is None or not hydrated.description_raw:
        return "waiting"

    listing.description_raw = hydrated.description_raw
    listing.published_at = hydrated.published_at or listing.published_at
    listing.content_hash = hydrated.content_hash
    try:
        # The run began hours ago. `_store` writes `closed_at = NULL`, so a
        # posting a crawl has closed since would be reopened by storing it,
        # and one a crawl has filled would be written over. Look again first.
        now = (
            await session.execute(
                select(Posting.closed_at, Posting.description_raw).where(
                    Posting.company_id == item.company_id,
                    Posting.external_id == listing.external_id,
                )
            )
        ).one_or_none()
        if now is None or now.closed_at is not None or now.description_raw:
            await session.commit()  # ends the read; commit keeps the cache usable
            return "skipped"
        company = companies.get(item.company_id) or await session.get(Company, item.company_id)
        if company is None:
            await session.commit()
            return "skipped"
        companies[item.company_id] = company
        await _store(session, company, [listing], now=datetime.now(UTC))
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - one bad row must not stop the host
        await session.rollback()
        # A rollback expires every cached company, not only this one.
        companies.clear()
        log.warning("backfill_store_failed", posting=listing.external_id, error=type(exc).__name__)
        return "waiting"
    return "filled"


async def backfill(
    fetcher: PoliteFetcher,
    sessions: Sessions,
    *,
    ats: Iterable[str] = tuple(READERS),
    limit: int | None = None,
    max_hosts: int = MAX_HOSTS,
    tried_before: frozenset[str] = frozenset(),
    retry_tried: bool = True,
    on_progress: Callable[[HostReport], None] | None = None,
    on_left_waiting: Callable[[str], None] | None = None,
    pace: Pace | None = None,
) -> BackfillReport:
    """Fill every missing description on the given boards, hosts side by side.

    The longest hosts start first: at one request a minute each, they decide
    when the run ends. Within a host, pages a previous run was refused or
    could not read (`tried_before`, by external id) come after the rest: a
    restart used to open every host with them, three refusals in a row.
    With `retry_tried` off they are not read at all and stay without text:
    nothing is written in their place, since an unknown stays NULL.
    """
    async with sessions() as session:
        pending = await find_pending(session, ats=ats)
    semaphore = asyncio.Semaphore(max_hosts)

    async def one(host: str, items: list[Pending]) -> HostReport:
        if retry_tried:
            queue = sorted(items, key=lambda item: item.listing.external_id in tried_before)
        else:
            queue = [item for item in items if item.listing.external_id not in tried_before]
        async with semaphore, sessions() as session:
            return await fill_host(
                fetcher,
                session,
                host,
                queue[:limit] if limit else queue,
                on_progress=on_progress,
                on_left_waiting=on_left_waiting,
                pace=pace,
            )

    ordered = sorted(pending.items(), key=lambda pair: -len(pair[1]))
    reports = await asyncio.gather(*(one(host, items) for host, items in ordered))
    return BackfillReport(hosts=list(reports))
