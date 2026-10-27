"""Boards whose list carries no description: read it, then a few job pages per crawl.

Workday, Breezy and Rippling all list a board's postings without their text,
and give each posting's text only on its own page. One request per posting at
§2.6's 60 s floor would make a 300-role board a five-hour read, so a crawl
spends a fixed budget of detail requests (`DETAIL_REQUESTS_PER_CRAWL`) and
caches what it read against the listed title, location and URL.

Missing or visibly changed postings take priority, with one rotating slot kept
for revalidating cached text, since a listing has no revision stamp to say a
description was edited. Once nothing is pending, every slot revalidates. The
two cursors live on `CompanyCrawlState` (`workday_detail_cursor`,
`workday_pending_cursor`): named for the first board that needed them, used by
every board here, and never shared because a company has one board.

A pending posting has no description: matching sees its title and location,
and no body embedding, until a later crawl fills it in and emits an update.

`strict` decides what one bad job page means. Workday's detail API is the
board's own JSON, so a failure there is the board failing and the crawl stops
(its long-standing behaviour). A Breezy or Rippling job page is ordinary HTML
that can close between the list and the detail request, so one bad page is
logged and that posting simply stays pending.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import structlog

from packages.crawler.extract import ExtractedPosting, posting_hash
from packages.crawler.fetch import Blocked, PoliteFetcher

log = structlog.get_logger(__name__)

# Five extra minutes at the company-host floor, never one request per job.
DETAIL_REQUESTS_PER_CRAWL = 5


@dataclass
class BoardPages:
    """Every page of a board, and what was parsed out of them."""

    #: Raw bodies in order. Hashed together for board-level change detection,
    #: which is why they are kept rather than discarded after parsing.
    bodies: list[str] = field(default_factory=list)
    postings: list[ExtractedPosting] = field(default_factory=list)
    waited_seconds: float = 0.0
    #: True when a page limit stopped the walk before the board ended, or the
    #: board disagreed with itself. The caller must not close anything then.
    truncated: bool = False

    @property
    def body(self) -> str:
        """One string standing for the whole board, for hashing."""
        # A detail-only edit must invalidate an otherwise identical listing.
        return "\n".join([*self.bodies, *(p.content_hash for p in self.postings)])


class DetailError(RuntimeError):
    """A job page could not be read, or did not belong to its listing."""


DetailUrl = Callable[[ExtractedPosting], str]
ReadDetail = Callable[[str, ExtractedPosting], ExtractedPosting]


async def hydrate(
    fetcher: PoliteFetcher,
    pages: BoardPages,
    previous: dict[str, ExtractedPosting],
    cursor: str | None,
    *,
    detail_url: DetailUrl,
    read_detail: ReadDetail,
    max_age_days: int,
    pending_cursor: str | None = None,
    strict: bool = True,
    board_error: type[Exception] = DetailError,
) -> tuple[str | None, str | None]:
    """Fill descriptions within the budget; return the two updated cursors.

    Cached text is reused only while its title, location and URL agree with
    the listing. With `strict`, a failed request or unreadable page raises
    `board_error`; without it, that posting is left pending and logged.
    """
    pending: list[ExtractedPosting] = []
    cached: list[ExtractedPosting] = []
    for item in pages.postings:
        prior = previous.get(item.external_id)
        same = prior is not None and (prior.title, prior.location, prior.url) == (
            item.title,
            item.location,
            item.url,
        )
        if prior is not None and item.published_at is None:
            item.published_at = prior.published_at
        if same and prior is not None and prior.description_raw:
            item.description_raw = prior.description_raw
            cached.append(item)
        elif prior is not None or not (
            max_age_days > 0
            and item.minimum_age_days is not None
            and item.minimum_age_days > max_age_days
        ):
            pending.append(item)
        item.content_hash = posting_hash(item)

    # Rotate even the unheld jobs: a detail that proves a job too old is
    # filtered by _store, so it appears pending again on the next crawl.
    pending.sort(key=lambda item: (item.external_id <= (pending_cursor or ""), item.external_id))
    cached.sort(key=lambda item: (item.external_id <= (cursor or ""), item.external_id))
    # Preserve a validation slot while draining the pending descriptions.
    reserved = 1 if cached else 0
    selected = pending[: DETAIL_REQUESTS_PER_CRAWL - reserved]
    selected += cached[: DETAIL_REQUESTS_PER_CRAWL - len(selected)]
    for item in selected:
        was_cached = item in cached
        hydrated: ExtractedPosting | None = None
        try:
            response = await fetcher.fetch(detail_url(item))
            pages.waited_seconds += response.waited
            if not response.ok:
                raise board_error(f"{response.url}: HTTP {response.status} in detail")
            hydrated = read_detail(response.text, item)
        except Blocked:
            # robots.txt refusing a job page refuses the board: never routed round.
            raise
        except Exception as exc:
            if strict:
                raise
            log.warning("detail_unreadable", posting=item.external_id, error=type(exc).__name__)
        if hydrated is not None:
            item.description_raw = hydrated.description_raw
            item.published_at = hydrated.published_at
            item.content_hash = hydrated.content_hash
        if was_cached:
            cursor = item.external_id
        else:
            pending_cursor = item.external_id
    return cursor, pending_cursor
