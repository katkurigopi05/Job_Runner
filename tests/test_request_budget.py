"""A ceiling on how many requests a crawl makes, shared across workers.

The gap this closes was found by hitting it. Asked for a bounded pilot —
twenty companies, two hundred requests, fifteen minutes — two of the three
bounds existed (`limit=20`, `timeout 900`) and the request one did not, so the
only honest answer was to say so.

The rate limiter answers "how fast", per host. This answers "how many", across
all of them.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text

from packages.crawler.budget import BudgetExhausted, reserve, state
from packages.crawler.fetch import PoliteFetcher
from packages.crawler.ratelimit import HostRateLimiter

WINDOW = 3600.0


class FakeClock:
    """Makes the §2.6 floor free without asking for a delay below it."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _limiter() -> HostRateLimiter:
    clock = FakeClock()
    return HostRateLimiter(clock=clock, sleeper=clock.sleep)


def _transport() -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        return httpx.Response(200, text="ok")

    return httpx.MockTransport(handle)


@pytest.fixture
def budgeted(monkeypatch, committing_sessionmaker):
    import packages.core.db as core_db

    monkeypatch.setattr(core_db, "get_sessionmaker", lambda: committing_sessionmaker)
    return committing_sessionmaker


async def test_a_zero_budget_means_unlimited(budgeted) -> None:
    """The shipped default. A ceiling that halts a sweep silently is worse
    than none — the symptom reads like the "board yields nothing" failure."""
    for _ in range(5):
        assert (await reserve(0, WINDOW)).limit == 0


async def test_the_budget_stops_at_its_ceiling(budgeted) -> None:
    for n in range(3):
        assert (await reserve(3, WINDOW)).requests == n + 1
    with pytest.raises(BudgetExhausted):
        await reserve(3, WINDOW)


async def test_a_refused_request_does_not_consume_a_slot(budgeted) -> None:
    """The counter is requests *made*, not requests attempted.

    Counting refusals would make the number climb while nothing reached the
    network, so the report would overstate what the crawl cost.
    """
    for _ in range(2):
        await reserve(2, WINDOW)
    for _ in range(4):
        with pytest.raises(BudgetExhausted):
            await reserve(2, WINDOW)

    assert (await state(2, WINDOW)).requests == 2


async def test_the_window_rolls_and_the_budget_returns(budgeted) -> None:
    """Without a window this is a lifetime quota, and a crawler that stops
    forever looks like a bug months later."""
    await reserve(1, WINDOW)
    with pytest.raises(BudgetExhausted):
        await reserve(1, WINDOW)

    # Age the window rather than sleep through it.
    async with budgeted() as session:
        await session.execute(
            text(
                "UPDATE crawler_request_budgets "
                "SET window_started_at = clock_timestamp() - interval '2 hours'"
            )
        )
        await session.commit()

    assert (await reserve(1, WINDOW)).requests == 1, "the window did not roll"


async def test_the_count_is_shared_rather_than_per_process(budgeted) -> None:
    """The defect §15 records for the rate limiter, not repeated here.

    Two fetchers stand in for two workers: a per-process ceiling of 2 would
    let them make four requests between them and tell nobody.
    """
    a = PoliteFetcher(transport=_transport(), rate_limiter=_limiter(), request_budget=2)
    b = PoliteFetcher(transport=_transport(), rate_limiter=_limiter(), request_budget=2)

    await a.fetch("https://one.example/x")
    await b.fetch("https://two.example/x")
    with pytest.raises(BudgetExhausted):
        await b.fetch("https://three.example/x")

    await a.aclose()
    await b.aclose()


async def test_the_fetcher_refuses_before_waiting_on_the_limiter(budgeted) -> None:
    """A request the budget will refuse must not first spend a minute queuing
    for a slot it will never use."""
    clock = FakeClock()
    fetcher = PoliteFetcher(
        transport=_transport(),
        rate_limiter=HostRateLimiter(clock=clock, sleeper=clock.sleep),
        request_budget=1,
    )

    await fetcher.fetch("https://one.example/a")
    before = clock.now
    with pytest.raises(BudgetExhausted):
        await fetcher.fetch("https://one.example/b")
    await fetcher.aclose()

    assert clock.now == before, (
        f"the refused request waited {clock.now - before}s on the limiter first"
    )


async def test_exhaustion_is_not_a_robots_refusal(budgeted) -> None:
    """`Blocked` is the site's decision and permanent for that URL; this is
    our own ceiling and the same URL succeeds next window. A caller that
    conflated them would retire a live board over a budget."""
    from packages.crawler.fetch import Blocked

    assert not issubclass(BudgetExhausted, Blocked)
