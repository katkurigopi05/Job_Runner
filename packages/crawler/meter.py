"""Where a crawl's wall-clock actually went, per host.

Written because the question "would changing X make it faster" could only be
answered by arithmetic over constants. The floors are known — 2s for the
shared ATS APIs, 60s for a company's own site (§2.6) — so anyone can multiply
them by a board count and get an estimate. What nobody could do was say what
the *network* cost, which is the half we do not control and the only half
where a speed-up is available at all.

So each request records two numbers against the host it finished on:

- **waited** — seconds inside the rate limiter. This is a rule, not a
  slowness. Making it smaller means breaking §2.6, which is out of scope by
  the same clause that forbids proxy rotation.
- **network** — seconds in the request itself, measured after both gates. A
  connection pool, a keep-alive, a smaller body: these move this number and
  only this number.

Reading them together is the point. A cycle that is 98% wait is behaving
correctly and cannot be tuned; one that is 40% network has something worth
looking at, and the split says which without anyone having to guess.

## Why the existing row rather than a new table

`crawler_host_budgets` is already one row per host, already written on every
request, and already shared across workers — which is the property that
matters, because `make workers n=4` means four processes and an in-process
counter would report a quarter of the truth to each of them.

## What this deliberately does not do

No per-request history. That is a row per fetch, forever, to answer a
question that only ever gets asked in aggregate — and CLAUDE.md §10 already
refuses to log page content, so a request log would have to be careful about
exactly the fields that would make it useful. Counters answer "where did the
time go" and cannot answer "what happened at 14:03", which is what the
structured log is for.

Failures are cheap and silent: a metric that takes down a crawl is worse than
no metric. Every write is wrapped, and an unreachable database costs the
counter rather than the fetch.
"""

from __future__ import annotations

import structlog
from sqlalchemy import text

from packages.core import db as core_db

log = structlog.get_logger(__name__)


async def record_request(host: str, *, waited: float, network: float) -> None:
    """Add one request's two costs to `host`'s running totals. Commits.

    Upserts rather than assuming the row exists: the shared limiter creates it
    on `acquire`, but `HostRateLimiter` — the in-process one, still used by
    tests and by anything constructed directly — does not, and a metric that
    only works under one limiter would quietly under-report the other.
    """
    try:
        sessionmaker = core_db.get_sessionmaker()
    except Exception:  # noqa: BLE001 — no database configured; nothing to record
        return

    try:
        async with sessionmaker() as session:
            await session.execute(
                text("""
                INSERT INTO crawler_host_budgets
                    (host, next_allowed_at, delay_seconds, requests, waited_seconds,
                     network_seconds, updated_at)
                VALUES
                    (:host, clock_timestamp(), 0, 1, :waited, :network, clock_timestamp())
                ON CONFLICT (host) DO UPDATE SET
                    requests        = crawler_host_budgets.requests + 1,
                    waited_seconds  = crawler_host_budgets.waited_seconds + :waited,
                    network_seconds = crawler_host_budgets.network_seconds + :network
                """),
                {"host": host, "waited": float(waited), "network": float(network)},
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 — never fail a fetch over a counter
        log.debug("host_metric_not_recorded", host=host, error=type(exc).__name__)
