"""Two numbers per host: the wait we chose, and the network we did not.

Written after "would changing the API methods make it faster" could only be
answered by multiplying constants — 181 boards times a 2s floor — because
nothing recorded what a request actually cost. The floors are knowable from
the source; the network is not, and it is the only half where a speed-up
exists at all.

The split is the whole value. A cycle that is 98% wait is §2.6 working
correctly and has nothing to tune; one that is 40% network has something worth
looking at. Neither number says that alone.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from packages.core.models import CrawlerHostBudget
from packages.crawler.fetch import PoliteFetcher
from packages.crawler.ratelimit import MIN_DELAY_SECONDS, HostRateLimiter


class FakeClock:
    """A clock the limiter's sleeper advances, so a 60s floor costs no time.

    §2.6 refuses a delay below its floor, so a test cannot ask for a fast
    limiter — it has to make the waiting free instead. The wait still happens
    and is still measured; only the wall clock is fictional.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _limiter() -> HostRateLimiter:
    clock = FakeClock()
    return HostRateLimiter(clock=clock, sleeper=clock.sleep)


def _transport(body: str = "hello", status: int = 200) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handle)


@pytest.fixture
def offline(monkeypatch, committing_sessionmaker):
    """A fetcher whose meter writes to the test database."""
    import packages.core.db as core_db

    monkeypatch.setattr(core_db, "get_sessionmaker", lambda: committing_sessionmaker)
    return PoliteFetcher(transport=_transport(), rate_limiter=_limiter())


async def _budget(sessions, host: str) -> CrawlerHostBudget | None:
    async with sessions() as session:
        return await session.scalar(select(CrawlerHostBudget).where(CrawlerHostBudget.host == host))


async def test_a_fetch_records_both_halves(offline, committing_sessionmaker) -> None:
    result = await offline.fetch("https://acme.example/board")
    await offline.aclose()

    assert result.ok
    row = await _budget(committing_sessionmaker, "acme.example")
    assert row is not None, "the fetch recorded nothing against its host"
    assert row.requests == 1
    assert row.network_seconds > 0, "a request that took no measurable time was not measured"
    assert row.waited_seconds >= 0


async def test_the_counters_accumulate_rather_than_overwrite(
    offline, committing_sessionmaker
) -> None:
    """Totals across a cycle are the question; a last-request gauge is not."""
    for _ in range(3):
        await offline.fetch("https://acme.example/board")
    await offline.aclose()

    row = await _budget(committing_sessionmaker, "acme.example")
    assert row.requests == 3


async def test_each_host_is_counted_separately(offline, committing_sessionmaker) -> None:
    """ "Which host is costing us" is the only actionable form of the question."""
    await offline.fetch("https://acme.example/a")
    await offline.fetch("https://beta.example/b")
    await offline.aclose()

    acme = await _budget(committing_sessionmaker, "acme.example")
    beta = await _budget(committing_sessionmaker, "beta.example")
    assert (acme.requests, beta.requests) == (1, 1)


async def test_the_wait_is_recorded_when_the_limiter_makes_us_wait(
    monkeypatch, committing_sessionmaker
) -> None:
    """The number that must never read as zero while a floor is being obeyed.

    A second request to the same host inside its delay is exactly the case
    §2.6 exists for, and if the meter reported it as free the report would
    say "all network" for a cycle that was almost all waiting.
    """
    import packages.core.db as core_db

    monkeypatch.setattr(core_db, "get_sessionmaker", lambda: committing_sessionmaker)
    fetcher = PoliteFetcher(transport=_transport(), rate_limiter=_limiter())

    await fetcher.fetch("https://slow.example/one")
    await fetcher.fetch("https://slow.example/two")
    await fetcher.aclose()

    row = await _budget(committing_sessionmaker, "slow.example")
    assert row.requests == 2
    assert row.waited_seconds >= MIN_DELAY_SECONDS * 0.9, (
        f"waited {row.waited_seconds:.1f}s across two requests to one host, whose floor is "
        f"{MIN_DELAY_SECONDS}s — the limiter's own cost is not reaching the counter, so the "
        f"report would call an almost-entirely-waiting cycle all network"
    )


async def test_a_metric_failure_never_fails_the_fetch(monkeypatch) -> None:
    """A counter that can take down a crawl is worse than no counter."""
    import packages.core.db as core_db

    def broken():
        raise RuntimeError("no database")

    monkeypatch.setattr(core_db, "get_sessionmaker", broken)
    fetcher = PoliteFetcher(transport=_transport(), rate_limiter=_limiter())

    result = await fetcher.fetch("https://acme.example/board")
    await fetcher.aclose()

    assert result.ok, "an unreachable metrics database stopped a fetch that was fine"
    assert result.network > 0
