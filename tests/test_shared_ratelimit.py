"""§2.6 across processes: two workers, one counter.

The in-process limiter is proven safe under concurrency in
`tests/test_ratelimit_concurrency.py`. That proof is about coroutines sharing
a dict, and it says nothing about two workers, because two workers do not
share a dict. N of them each keep their own idea of when a host was last
touched, so the effective floor becomes the floor divided by N — and the only
symptom on this side is that the crawl got faster.

Every limiter here is built with its **own sessionmaker**, on purpose. Sharing
one would let SQLAlchemy's identity map and transaction do work the real
arrangement cannot: separate workers are separate processes with separate
pools, and the only thing they have in common is the table.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from packages.core.models import CrawlerHostBudget
from packages.crawler.host_budget import SharedHostRateLimiter
from packages.crawler.ratelimit import (
    MIN_DELAY_SECONDS,
    MIN_SHARED_API_DELAY_SECONDS,
    RateLimitTooLow,
)
from tests.conftest import TEST_DATABASE_URL

SHARED = "boards-api.greenhouse.io"
OTHER_SHARED = "api.lever.co"
COMPANY = "careers.acme.example"


class Recorder:
    """A sleeper that records rather than waits.

    The waits under test are 2s and 60s of real time. What matters is *how
    long a caller was told to wait*, which is exactly what the limiter
    returns, so nothing here needs to actually pass.
    """

    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


@pytest.fixture
async def worker_sessions():
    """A factory for limiters that share only the database.

    Each call builds a fresh engine, so two limiters from this fixture have
    nothing in common but the table — which is the situation being tested.
    """
    engines = []

    def build(**kwargs) -> SharedHostRateLimiter:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=None)
        engines.append(engine)
        return SharedHostRateLimiter(
            sessions=async_sessionmaker(engine, expire_on_commit=False),
            sleeper=Recorder(),
            **kwargs,
        )

    try:
        yield build
    finally:
        for engine in engines:
            await engine.dispose()


@pytest.fixture(autouse=True)
async def _clear_budgets(engine):
    async with engine.begin() as conn:
        await conn.execute(sa.text("TRUNCATE crawler_host_budgets"))
    yield


# --------------------------------------------------------------------------
# The floor is still a floor
# --------------------------------------------------------------------------


def test_a_delay_below_the_floor_is_refused() -> None:
    """Refused, not clamped — the same rule as the in-process limiter."""
    with pytest.raises(RateLimitTooLow):
        SharedHostRateLimiter(delay_seconds=5.0)


def test_a_shared_api_override_below_its_floor_is_refused() -> None:
    with pytest.raises(RateLimitTooLow):
        SharedHostRateLimiter(host_delays={OTHER_SHARED: 0.1})


def test_a_shared_api_host_keeps_the_faster_floor() -> None:
    """`max(delay_seconds, floor)` here would undo the whole §2.6 amendment.

    `delay_seconds` is the 60s company-host floor. Taking the larger of the
    two would give every shared ATS API 60 seconds, putting the crawler back
    to 60 boards an hour however many companies are listed.
    """
    limiter = SharedHostRateLimiter(delay_seconds=MIN_DELAY_SECONDS)

    assert limiter.delay_for(SHARED) == MIN_SHARED_API_DELAY_SECONDS
    assert limiter.delay_for(COMPANY) == MIN_DELAY_SECONDS


# --------------------------------------------------------------------------
# Two workers, one counter
# --------------------------------------------------------------------------


async def test_two_workers_on_one_host_do_not_both_go_at_once(worker_sessions) -> None:
    """The whole reason this class exists.

    With a dict per process both workers read "no request yet" and both fire.
    Here the second one is told to wait a full floor, because the first took
    the slot.
    """
    first, second = worker_sessions(), worker_sessions()

    assert await first.acquire(SHARED) == 0.0, "nobody has touched it; go now"
    waited = await second.acquire(SHARED)

    assert waited >= MIN_SHARED_API_DELAY_SECONDS * 0.9, (
        f"a second worker was let through after {waited:.3f}s — "
        "the floor is being counted per process"
    )


async def test_workers_on_different_hosts_do_not_wait_on_each_other(worker_sessions) -> None:
    """Separate hosts keep separate counters; §2.6 is written per host."""
    first, second = worker_sessions(), worker_sessions()

    await first.acquire(SHARED)
    waited = await second.acquire(OTHER_SHARED)

    assert waited == 0.0, "a different host was made to wait"


async def test_a_company_host_gets_the_full_sixty_seconds(worker_sessions) -> None:
    first, second = worker_sessions(), worker_sessions()

    await first.acquire(COMPANY)
    waited = await second.acquire(COMPANY)

    assert waited >= MIN_DELAY_SECONDS * 0.9


async def test_many_workers_are_spaced_cumulatively(worker_sessions) -> None:
    """The tenth caller waits nine floors, not one.

    A limiter that let everyone through after a single floor would still look
    like it was waiting.
    """
    waits = [await worker_sessions().acquire(SHARED) for _ in range(10)]

    assert waits[0] == 0.0
    for index, waited in enumerate(waits[1:], start=1):
        expected = MIN_SHARED_API_DELAY_SECONDS * index
        assert waited >= expected * 0.85, f"caller {index} waited {waited:.2f}s, wanted {expected}s"


async def test_concurrent_workers_take_distinct_slots(worker_sessions) -> None:
    """Arriving together must not mean going together.

    Each reservation is one statement, so the second cannot read the marker
    until the first has committed its push — which is the property a
    read-then-write pair could not have.
    """
    limiters = [worker_sessions() for _ in range(5)]

    waits = sorted(await asyncio.gather(*(limiter.acquire(SHARED) for limiter in limiters)))

    assert waits[0] == 0.0
    assert len(set(waits)) == 5, f"slots collided: {waits}"
    for earlier, later in zip(waits, waits[1:], strict=False):
        assert later - earlier >= MIN_SHARED_API_DELAY_SECONDS * 0.85


# --------------------------------------------------------------------------
# Listening: the half that makes the faster floor defensible
# --------------------------------------------------------------------------


async def test_a_429_backs_every_worker_off(worker_sessions) -> None:
    """§2.6 permits the 2s shared floor only "while also listening".

    A backoff one worker records and another walks around is not listening.
    """
    saw_it, other = worker_sessions(), worker_sessions()

    await saw_it.penalize(SHARED, 300.0)
    waited = await other.acquire(SHARED)

    assert waited >= 299.0, f"another worker slipped past the penalty after {waited:.1f}s"


async def test_a_penalty_only_ever_extends(worker_sessions) -> None:
    """A server asking for a shorter pause does not shorten ours."""
    limiter = worker_sessions()

    await limiter.penalize(SHARED, 300.0)
    await limiter.penalize(SHARED, 5.0)

    assert await worker_sessions().acquire(SHARED) >= 299.0


async def test_a_crawl_delay_read_by_one_worker_binds_the_others(worker_sessions) -> None:
    """A rule in robots.txt is the site's, not one worker's."""
    reader, other = worker_sessions(), worker_sessions()

    await reader.raise_delay(SHARED, 30.0)
    await other.acquire(SHARED)
    waited = await worker_sessions().acquire(SHARED)

    assert waited >= 29.0, "the site asked for 30s and another worker used 2s"


async def test_a_crawl_delay_below_the_floor_buys_nothing(worker_sessions) -> None:
    """Configurable upward only — asking for less is not a way in.

    Not an error, and deliberately so: a site publishing `Crawl-delay: 5` is
    making a perfectly ordinary request that happens to be looser than our own
    rule, and the answer to it is to keep waiting 60 seconds rather than to
    fail the crawl.
    """
    limiter = worker_sessions()

    await limiter.raise_delay(COMPANY, 5.0)

    assert limiter.delay_for(COMPANY) == MIN_DELAY_SECONDS
    await limiter.acquire(COMPANY)
    assert await worker_sessions().acquire(COMPANY) >= MIN_DELAY_SECONDS * 0.9


async def test_a_shorter_crawl_delay_changes_nothing(worker_sessions) -> None:
    """Below what we already apply is simply ignored, not an error."""
    limiter = worker_sessions()

    await limiter.raise_delay(SHARED, 1.0)

    assert limiter.delay_for(SHARED) == MIN_SHARED_API_DELAY_SECONDS


async def test_a_redirect_hop_costs_a_slot(worker_sessions) -> None:
    """A same-host hop still cost the server a round trip."""
    limiter = worker_sessions()

    await limiter.acquire(SHARED)
    await limiter.record(SHARED)
    waited = await worker_sessions().acquire(SHARED)

    assert waited >= MIN_SHARED_API_DELAY_SECONDS * 2 * 0.85, (
        "the extra request was not counted against the host"
    )


async def test_the_marker_is_stored_against_the_host(db_session, worker_sessions) -> None:
    """Keyed on the host, which is what §2.6 counts requests against."""
    await worker_sessions().acquire(SHARED)

    row = await db_session.scalar(
        sa.select(CrawlerHostBudget).where(CrawlerHostBudget.host == SHARED)
    )
    assert row is not None
    assert row.delay_seconds == MIN_SHARED_API_DELAY_SECONDS
    assert row.next_allowed_at > datetime.now(UTC)


async def test_waiting_happens_outside_a_transaction(worker_sessions) -> None:
    """A 60s wait must not hold a row lock, or the pool deadlocks first.

    If `acquire` slept with the row locked, the second caller's reservation
    would block for the whole wait instead of returning promptly with a
    deadline of its own.
    """
    first, second = worker_sessions(), worker_sessions()
    await first.acquire(COMPANY)

    started = time.monotonic()
    waited = await second.acquire(COMPANY)
    elapsed = time.monotonic() - started

    assert waited >= MIN_DELAY_SECONDS * 0.9, "it should have been told to wait a full minute"
    assert elapsed < 5.0, (
        f"reserving took {elapsed:.1f}s of real time — the lock is held while waiting"
    )


# --------------------------------------------------------------------------
# The default, which is the part that was actually wrong
# --------------------------------------------------------------------------


async def test_build_fetcher_shares_the_counter_by_default(engine) -> None:
    """Two fetchers built the ordinary way must not hold two counters.

    This is the defect, not a refinement of it. `build_fetcher()` constructs a
    fresh limiter on every call, so before the default changed, two concurrent
    tasks in *one process* each had their own `_last_request` dict and both
    fired immediately against a host owed 2s. Measured, as the settings
    docstring records.

    `make workers n=4` is a documented command that runs four claimants in one
    process, so the shipped default was up to 4× the permitted rate on every
    path except the single handler that passed `shared=True` by hand. A test
    that only ever built limiters explicitly could not see it — which is why
    this one goes through `build_fetcher`.
    """
    from packages.crawler.fetch import build_fetcher
    from packages.crawler.host_budget import SharedHostRateLimiter

    first, second = build_fetcher(), build_fetcher()
    try:
        assert isinstance(first.rate_limiter, SharedHostRateLimiter), (
            "the default must be the shared limiter"
        )
        assert first.rate_limiter is not second.rate_limiter, (
            "separate objects is the point — they have to agree through the table"
        )

        assert await first.rate_limiter.acquire(SHARED) == 0.0
        waited = await second.rate_limiter.acquire(SHARED)

        # The reservation hands back "time from now until your slot", so a few
        # elapsed milliseconds put it just under the floor. The spacing between
        # the two requests is the full floor, which is what §2.6 is about.
        assert waited >= MIN_SHARED_API_DELAY_SECONDS * 0.95, (
            f"a second fetcher built the same way waited only {waited:.3f}s"
        )
    finally:
        await first.aclose()
        await second.aclose()


async def test_four_concurrent_fetchers_are_spaced_by_the_floor(engine) -> None:
    """`make workers n=4`, measured end to end rather than per-call.

    Asserts the gaps between requests, because that is the quantity §2.6
    names. Four tasks that each waited and then fired together have waited and
    still breached it.
    """
    import asyncio
    import time

    from packages.crawler.fetch import build_fetcher

    fetchers = [build_fetcher() for _ in range(4)]
    stamps: list[float] = []

    async def hit(fetcher) -> None:
        await fetcher.rate_limiter.acquire(SHARED)
        stamps.append(time.monotonic())

    try:
        await asyncio.gather(*(hit(fetcher) for fetcher in fetchers))
        stamps.sort()
        gaps = [later - earlier for earlier, later in zip(stamps, stamps[1:], strict=False)]
        assert len(gaps) == 3
        for gap in gaps:
            assert gap >= MIN_SHARED_API_DELAY_SECONDS * 0.95, (
                f"requests were {gap:.3f}s apart, under the {MIN_SHARED_API_DELAY_SECONDS}s floor"
            )
    finally:
        for fetcher in fetchers:
            await fetcher.aclose()
