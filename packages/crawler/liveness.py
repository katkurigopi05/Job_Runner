"""Is this posting still open?

Registry postings answer this for free: the crawler re-reads the whole board,
and anything that stops appearing gets `closed_at` set. Aggregator postings
have no board to re-read. They arrive one at a time from a feed that may be
serving something filled three months ago, and nothing in discovery ever went
back to check — so a dead job sat in the match feed looking exactly like a
live one.

This is the check that was missing. It is deliberately cheap: an HTTP fetch
and a look at what came back. No browser.

## What counts as closed, and what does not

`404` and `410` are the site saying the posting is gone, and the ATS board
APIs are consistent about it. Beyond that a handful of unambiguous phrases —
"no longer accepting applications" — appear on the closed-posting page of
every ATS we support.

Everything else is `UNKNOWN`, and `UNKNOWN` never closes a posting. A network
blip, a redirect to a login wall, a rate limit, an unreachable robots.txt: all
of those are us failing to see the posting, not the posting being gone. This
is the same lesson `_close_missing` learned in `crawl.py` — a fetch we could
not read is not evidence of a closure, and treating it as one deletes real
jobs out of the feed with no error anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import structlog

from packages.crawler.fetch import Blocked, PoliteFetcher

log = structlog.get_logger(__name__)

#: Phrases an ATS puts on a posting that has been filled or withdrawn.
CLOSED_MARKERS = (
    "no longer accepting applications",
    "this job is no longer available",
    "this position has been filled",
    "this posting has been closed",
    "job posting not found",
    "position is no longer open",
    "we are no longer accepting",
)


class State(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    #: We could not tell. Never acted on.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Liveness:
    state: State
    reason: str

    @property
    def is_closed(self) -> bool:
        return self.state is State.CLOSED


def read_response(status: int, body: str) -> Liveness:
    """Classify a fetched posting page. Pure, so it is testable offline."""
    if status in (404, 410):
        return Liveness(State.CLOSED, f"HTTP {status}")

    if status >= 400:
        # Includes 429 and 5xx: the site is refusing us, not retiring the job.
        return Liveness(State.UNKNOWN, f"HTTP {status}")

    lowered = body.lower()
    for marker in CLOSED_MARKERS:
        if marker in lowered:
            return Liveness(State.CLOSED, f"page says {marker!r}")

    if not lowered.strip():
        return Liveness(State.UNKNOWN, "empty body")

    return Liveness(State.OPEN, f"HTTP {status}, no closure marker")


async def check(url: str, fetcher: PoliteFetcher) -> Liveness:
    """Fetch one posting and say whether it is still open."""
    try:
        response = await fetcher.fetch(url)
    except Blocked as exc:
        return Liveness(State.UNKNOWN, f"blocked: {exc}")
    except Exception as exc:  # noqa: BLE001 - unreachable is not closed
        return Liveness(State.UNKNOWN, f"fetch failed: {type(exc).__name__}")

    return read_response(response.status, response.text)
