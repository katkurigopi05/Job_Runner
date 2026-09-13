"""A ceiling on how many requests a crawl may make. CLAUDE.md §2.6's other half.

The rate limiter answers "how fast", per host. Nothing answered "how many", at
all. That gap was concrete: asked for a bounded pilot — twenty companies, two
hundred requests, fifteen minutes — the first two were expressible (`limit=20`,
`timeout 900`) and the third simply was not, so the honest answer was that the
request bound did not exist.

## Why it lives behind the fetcher

Same argument `fetch.py` already makes about robots and the floor: a control
enforced at each call site is one a new extractor can forget. `PoliteFetcher`
is the only way this project reaches the network, so a budget checked there is
one nothing can route around.

## Why it is shared rather than counted in the process

Exactly the defect §15 records for the rate limiter: `make workers n=4` is four
processes, so a per-process ceiling of 200 is a real ceiling of 800 and nobody
is told. The count lives in one row and every worker reserves from it.

## Why it is off by default

A budget that silently halts a crawl is worse than no budget — the symptom is a
sweep that stops early, which reads exactly like the "board yields nothing"
failure this repo has been bitten by twice. `CRAWLER_REQUEST_BUDGET=0` means
unlimited, which is the shipped behaviour, and turning it on is a deliberate
act for a run you are watching.

## The window

Rolling, and reset lazily on first use after it lapses rather than by a job.
A budget with no window is a lifetime quota: the crawler works until it stops
forever, which nobody wants and which would look like a bug months later.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy import text

from packages.core import db as core_db

log = structlog.get_logger(__name__)


class BudgetExhausted(Exception):
    """The request ceiling for the current window is spent.

    Deliberately not a `Blocked`. Being refused by robots is the site's
    decision and a permanent property of that URL; this is our own ceiling and
    the same URL succeeds in the next window. Callers that treat every refusal
    as "this board is unreachable" would retire a live board over a budget.
    """


@dataclass(frozen=True)
class BudgetState:
    requests: int
    limit: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.requests)


#: One statement: roll the window if it has lapsed, then take a slot only if
#: one is left. `clock_timestamp()` rather than `now()` for the reason
#: `crawler_host_budgets` gives — `now()` is the transaction's start time, so
#: every statement inside one transaction would read the same instant.
#:
#: The `WHERE` is what makes it a reservation rather than a read-then-write:
#: two workers arriving together cannot both see 199 and both proceed.
_RESERVE = text("""
    INSERT INTO crawler_request_budgets (id, window_started_at, requests)
    VALUES (1, clock_timestamp(), 1)
    ON CONFLICT (id) DO UPDATE SET
        window_started_at = CASE
            WHEN crawler_request_budgets.window_started_at
                 < clock_timestamp() - make_interval(secs => :window)
            THEN clock_timestamp()
            ELSE crawler_request_budgets.window_started_at
        END,
        requests = CASE
            WHEN crawler_request_budgets.window_started_at
                 < clock_timestamp() - make_interval(secs => :window)
            THEN 1
            ELSE crawler_request_budgets.requests + 1
        END
    WHERE
        crawler_request_budgets.window_started_at
            < clock_timestamp() - make_interval(secs => :window)
        OR crawler_request_budgets.requests < :limit
    RETURNING requests
""")


async def reserve(limit: int, window_seconds: float) -> BudgetState:
    """Take one request from the budget, or raise `BudgetExhausted`.

    A refused request never reaches the network and never consumes a slot —
    the `WHERE` declines the update rather than counting past the ceiling, so
    the stored number stays a true count of requests made rather than of
    requests attempted.
    """
    if limit <= 0:
        return BudgetState(requests=0, limit=0)

    async with core_db.get_sessionmaker()() as session:
        row = (
            await session.execute(_RESERVE, {"limit": limit, "window": float(window_seconds)})
        ).first()
        await session.commit()

    if row is None:
        log.warning("request_budget_exhausted", limit=limit, window_s=window_seconds)
        raise BudgetExhausted(
            f"the crawl has made its {limit} requests for this window; "
            f"raise CRAWLER_REQUEST_BUDGET or wait for the window to roll"
        )
    return BudgetState(requests=int(row[0]), limit=limit)


async def state(limit: int, window_seconds: float) -> BudgetState:
    """What the budget currently reads, reserving nothing.

    For reporting. Never use it to decide whether to fetch: between this read
    and the request another worker may take the last slot, which is the
    check-then-act race `reserve` exists to close.
    """
    async with core_db.get_sessionmaker()() as session:
        row = (
            await session.execute(
                text("""
                SELECT CASE
                    WHEN window_started_at < clock_timestamp() - make_interval(secs => :window)
                    THEN 0 ELSE requests
                END
                FROM crawler_request_budgets WHERE id = 1
                """),
                {"window": float(window_seconds)},
            )
        ).first()
    return BudgetState(requests=int(row[0]) if row else 0, limit=limit)


__all__ = ["BudgetExhausted", "BudgetState", "reserve", "state"]
