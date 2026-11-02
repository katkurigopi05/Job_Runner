"""The backfill's pace: §2.6 amended for one owner-approved run (2026-11-02).

The owner asked for job pages at 5 s a host, stepping to 10, 15 and 20 if
the sites push back. These hold that it never goes below what was approved,
only ever slows down, stops when even the slowest step is refused, and still
gives way to a site's own Crawl-delay and a 429.
"""

from __future__ import annotations

import pytest

from packages.crawler.backfill import MIN_BACKFILL_DELAY_SECONDS, Pace, PacedRateLimiter
from packages.crawler.ratelimit import RateLimitTooLow
from tests.test_workday import FakeClock

HOST = "acme.wd5.myworkdayjobs.com"


def test_a_step_below_the_approved_floor_is_refused():
    with pytest.raises(RateLimitTooLow):
        Pace(steps=(MIN_BACKFILL_DELAY_SECONDS - 1, 10.0))


def test_steps_only_slow_down():
    with pytest.raises(ValueError, match="slower"):
        Pace(steps=(10.0, 5.0))


def test_it_steps_up_when_many_recent_pages_are_refused_and_never_back_down():
    pace = Pace(steps=(5.0, 10.0, 15.0, 20.0), window=10)
    for _ in range(10):
        pace.record(refused=False)
    assert pace.current == 5.0
    for _ in range(7):
        pace.record(refused=False)
    for _ in range(3):
        pace.record(refused=True)
    assert pace.current == 10.0, "3 of the last 10 refused"
    for _ in range(30):
        pace.record(refused=False)
    assert pace.current == 10.0, "never speeds back up"


def test_refused_at_the_slowest_step_it_stops():
    pace = Pace(steps=(5.0, 10.0), window=4)
    for _ in range(8):
        pace.record(refused=True)
    assert pace.current == 10.0
    assert pace.stopped


async def test_a_host_is_spaced_by_the_current_step():
    clock = FakeClock()
    pace = Pace(steps=(5.0, 10.0), window=4)
    limiter = PacedRateLimiter(pace=pace, clock=clock, sleeper=clock.sleep)
    await limiter.acquire(HOST)
    assert await limiter.acquire(HOST) == pytest.approx(5.0)
    for _ in range(4):
        pace.record(refused=True)
    assert await limiter.acquire(HOST) == pytest.approx(10.0)


async def test_a_sites_crawl_delay_and_a_429_still_win():
    clock = FakeClock()
    limiter = PacedRateLimiter(pace=Pace(steps=(5.0,)), clock=clock, sleeper=clock.sleep)
    await limiter.raise_delay(HOST, 30.0)
    assert limiter.delay_for(HOST) == 30.0
    await limiter.acquire(HOST)
    await limiter.penalize(HOST, 120.0)
    assert await limiter.acquire(HOST) == pytest.approx(120.0)


def test_the_crawlers_own_limiter_still_refuses_below_sixty():
    from packages.crawler.ratelimit import HostRateLimiter

    with pytest.raises(RateLimitTooLow):
        HostRateLimiter(delay_seconds=5.0)
