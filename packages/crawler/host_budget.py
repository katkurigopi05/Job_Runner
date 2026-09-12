"""§2.6 enforced across processes, not just inside one.

`HostRateLimiter` holds its counters in a dict. That is correct and enough for
exactly as long as one process does all the crawling. Dispatch companies to
several workers and each gets its own dict and its own idea of when a host was
last touched, so N workers make the effective floor the floor divided by N —
and the traffic simply gets faster, which is the kind of breach nobody on this
side ever notices.

This implements the same `RateLimiter` interface against a table, so the
counter is shared. Everything §2.6 requires is kept:

- The floor is refused below, never clamped, on construction and per host.
- A site's own `Crawl-delay` still raises that host's delay, and now binds
  every worker rather than the one that happened to read robots.txt.
- A 429 or `Retry-After` backs the host off for everyone, and only ever
  extends.

## Reservations, not last-request stamps

The dict version records *when the last request happened* and each caller
works out whether enough time has passed. Two processes doing that both read
the same stamp and both conclude they may go — a check-then-act race that no
amount of care on either side closes, because the read and the write are
separate round trips.

So the table stores `next_allowed_at`, the next instant at which some caller
may proceed, and a caller *takes* the slot: one statement reads the marker,
returns the slot to its caller, and pushes the marker out by one delay. Two
workers arriving together get two slots a floor apart, because the second
statement cannot run until the first has committed its row.

The waiting then happens outside any transaction. Holding a row lock for the
60 seconds a company host is owed would idle a database connection per host
and deadlock the pool long before it broke politeness.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.crawler.ratelimit import (
    MIN_DELAY_SECONDS,
    MIN_SHARED_API_DELAY_SECONDS,
    SHARED_API_HOSTS,
    RateLimitTooLow,
    floor_for,
)

log = structlog.get_logger(__name__)

#: Take the next free slot and push the marker one delay past it.
#:
#: `GREATEST(next_allowed_at, clock_timestamp())` is the slot: the marker if
#: the host is still owed time, otherwise now. `clock_timestamp()` rather than
#: `now()` throughout — `now()` is the transaction's start time, identical for
#: every statement inside it, so a cycle would hand every host the same
#: instant and the spacing would be between transactions rather than requests.
#:
#: The effective delay is `GREATEST` of the stored one and the caller's, so a
#: `Crawl-delay` another worker recorded cannot be undercut by a caller that
#: has not read robots.txt for this host yet.
_RESERVE = text("""
    INSERT INTO crawler_host_budgets AS b (host, next_allowed_at, delay_seconds, updated_at)
    VALUES (
        :host,
        clock_timestamp() + make_interval(secs => :delay),
        :delay,
        clock_timestamp()
    )
    ON CONFLICT (host) DO UPDATE
       SET next_allowed_at = GREATEST(b.next_allowed_at, clock_timestamp())
                           + make_interval(secs => GREATEST(b.delay_seconds, :delay)),
           delay_seconds   = GREATEST(b.delay_seconds, :delay),
           updated_at      = clock_timestamp()
    RETURNING next_allowed_at, delay_seconds, clock_timestamp() AS observed_now
""")

#: An extra request that `acquire` did not reserve — a same-host redirect hop.
#: It still cost the server a round trip, so it takes a slot of its own.
_CONSUME = text("""
    INSERT INTO crawler_host_budgets AS b (host, next_allowed_at, delay_seconds, updated_at)
    VALUES (
        :host,
        clock_timestamp() + make_interval(secs => :delay),
        :delay,
        clock_timestamp()
    )
    ON CONFLICT (host) DO UPDATE
       SET next_allowed_at = GREATEST(b.next_allowed_at, clock_timestamp())
                           + make_interval(secs => GREATEST(b.delay_seconds, :delay)),
           updated_at      = clock_timestamp()
""")

#: A 429 or `Retry-After`. `GREATEST` is what makes it "only ever extends" — a
#: server asking for a shorter pause than we already planned does not get to
#: shorten it.
_PENALIZE = text("""
    INSERT INTO crawler_host_budgets AS b (host, next_allowed_at, delay_seconds, updated_at)
    VALUES (
        :host,
        clock_timestamp() + make_interval(secs => :seconds),
        :delay,
        clock_timestamp()
    )
    ON CONFLICT (host) DO UPDATE
       SET next_allowed_at = GREATEST(
               b.next_allowed_at,
               clock_timestamp() + make_interval(secs => :seconds)
           ),
           updated_at      = clock_timestamp()
""")

#: A site's `Crawl-delay`. Upward only, here as everywhere.
_RAISE_DELAY = text("""
    INSERT INTO crawler_host_budgets AS b (host, next_allowed_at, delay_seconds, updated_at)
    VALUES (:host, clock_timestamp(), :seconds, clock_timestamp())
    ON CONFLICT (host) DO UPDATE
       SET delay_seconds = GREATEST(b.delay_seconds, :seconds),
           updated_at    = clock_timestamp()
""")


@dataclass
class SharedHostRateLimiter:
    """A `RateLimiter` whose counters live in Postgres, shared by every worker."""

    delay_seconds: float = MIN_DELAY_SECONDS
    #: Makes short-lived sessions of its own. Deliberately *not* the caller's
    #: session: a reservation has to be committed to be visible to another
    #: worker, and the crawl's session commits when the whole task ends —
    #: which is hours later, and rolls back entirely if the cycle fails. A
    #: reservation that vanishes on rollback is a floor that vanishes with it.
    sessions: async_sessionmaker[AsyncSession] | None = None
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep
    #: Per-host overrides known to *this* process. A hint, not the authority:
    #: the stored value is folded in with `GREATEST` inside every statement,
    #: so a delay another worker recorded is honoured whether or not it is
    #: cached here.
    host_delays: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.delay_seconds < MIN_DELAY_SECONDS:
            raise RateLimitTooLow(self.delay_seconds)
        for host, delay in self.host_delays.items():
            floor = floor_for(host)
            if delay < floor:
                raise RateLimitTooLow(delay, floor)

    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        if self.sessions is None:
            from packages.core.db import get_sessionmaker

            self.sessions = get_sessionmaker()
        return self.sessions

    def delay_for(self, host: str) -> float:
        """The delay this process knows about for `host`.

        Deliberately identical to `HostRateLimiter.delay_for`, including the
        part that looks like a mistake: a shared ATS host gets
        `MIN_SHARED_API_DELAY_SECONDS`, *not* the larger of that and the
        configured delay. `delay_seconds` is the company-host floor of 60s, so
        taking the maximum would hand every shared ATS API 60 seconds and undo
        the whole §2.6 amendment — the crawler would be back to 60 boards an
        hour no matter how many companies are listed.

        Used to decide whether a `Crawl-delay` is worth writing down. The
        enforced value is whatever the table holds, which may be larger.
        """
        if host in self.host_delays:
            return self.host_delays[host]
        if host in SHARED_API_HOSTS:
            return MIN_SHARED_API_DELAY_SECONDS
        return self.delay_seconds

    async def acquire(self, host: str) -> float:
        """Reserve the next slot for `host`, wait for it, and return the wait."""
        delay = self.delay_for(host)
        async with self._sessions()() as session:
            row = (await session.execute(_RESERVE, {"host": host, "delay": delay})).one()
            await session.commit()

        # The statement returned the marker *after* it was pushed out, so the
        # slot this caller was given is one effective delay behind it.
        slot = row.next_allowed_at - timedelta(seconds=float(row.delay_seconds))
        waited = float((slot - row.observed_now).total_seconds())
        if waited <= 0:
            return 0.0
        log.debug("rate_limit_wait", host=host, seconds=round(waited, 1))
        await self.sleeper(waited)
        return waited

    async def record(self, host: str) -> None:
        """Account for a request `acquire` did not reserve."""
        async with self._sessions()() as session:
            await session.execute(_CONSUME, {"host": host, "delay": self.delay_for(host)})
            await session.commit()

    async def penalize(self, host: str, seconds: float) -> None:
        """Back `host` off for at least `seconds`, for every worker."""
        if seconds <= 0:
            return
        async with self._sessions()() as session:
            await session.execute(
                _PENALIZE,
                {"host": host, "seconds": seconds, "delay": self.delay_for(host)},
            )
            await session.commit()
        log.info("rate_limit_penalty", host=host, seconds=round(seconds, 1))

    async def raise_delay(self, host: str, seconds: float) -> None:
        """Honour a site's `Crawl-delay`, and share it with the other workers.

        Nothing is refused here, and the absence of a floor check is the
        point rather than an omission. `delay_for` never returns less than the
        host's floor — `__post_init__` validates every override, and this
        method only ever writes a value larger than the one it just compared
        against — so anything that gets past the line above is already above
        the floor. A check there could not fire, and a safety check that
        cannot fire is worse than none: it reads as protection.
        """
        if seconds <= self.delay_for(host):
            return
        async with self._sessions()() as session:
            await session.execute(_RAISE_DELAY, {"host": host, "seconds": float(seconds)})
            await session.commit()
        self.host_delays[host] = float(seconds)
