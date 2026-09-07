"""The §2.6 floor holds under concurrency — established before relying on it.

`find_boards` probes four ATS hosts per company and paid the *sum* of their
floors because it probed them one after another. Probing them at once costs
the slowest host instead, which is the difference between a 16-hour sweep of
the owner's 3,802-company CSV and roughly four hours. Nothing about §2.6
objects: the rule is written per host, and four hosts have four floors.

Concurrency only buys that if `acquire` is safe under it, so this file was
written first, to find out rather than to assume.

**It is safe, and this file is the evidence rather than a bug fix.** The
suspicion was a check-then-act race: `acquire` reads readiness, awaits a
sleep, and records the request. But the `await` runs *only* on the branch
where the host is not ready, the loop re-checks after it, and between the
final check and `record` there is no await at all — so under asyncio's
cooperative scheduling nothing can interleave there. A per-host lock was
written, measured against these tests, and reverted as unnecessary.

Kept because the property is now load-bearing. `resolve_one` fans out across
vendors on the strength of it, and a future change to `acquire` that
introduced an await between the check and the record would breach the floor
silently — the traffic would simply get faster, and the people who find out
would be at the far end.

## Why a scaled real clock rather than the usual fake one

The `FakeClock` the other crawler tests use advances a shared counter inside
`sleep`, so two coroutines sleeping "at the same time" advance it twice and
come out serialised. That is the opposite of what real concurrent sleeps do,
and it makes these assertions vacuous — the first draft of this file passed
identically with and without the lock, which is a test of nothing.

So time here is real asyncio time, scaled: one limiter-second is 10ms of wall
clock. Concurrent sleeps genuinely overlap and the file still runs in under a
second.

Gaps are asserted against `_FLOOR_TOLERANCE` of the floor rather than the
floor exactly, because `asyncio.sleep` is not millisecond-precise. The
tolerance is nowhere near wide enough to hide a breach: a limiter that let
callers through together would show a gap of approximately zero.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from packages.crawler.ratelimit import MIN_SHARED_API_DELAY_SECONDS, HostRateLimiter

SHARED = "boards-api.greenhouse.io"
OTHER_SHARED = "api.lever.co"

#: Limiter-seconds per real second. A 2s floor becomes 20ms of waiting.
_SCALE = 100.0

#: Scheduler jitter is milliseconds; the bug is a gap of ~0. Anything between
#: the two separates them, and this stays close to the floor it stands in for.
_FLOOR_TOLERANCE = 0.85


class ScaledClock:
    """Real asyncio time, sped up, so concurrent sleeps really do overlap."""

    def __init__(self) -> None:
        self._origin = time.monotonic()

    def __call__(self) -> float:
        return (time.monotonic() - self._origin) * _SCALE

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds / _SCALE)


def _limiter() -> tuple[HostRateLimiter, ScaledClock]:
    clock = ScaledClock()
    return HostRateLimiter(clock=clock, sleeper=clock.sleep), clock


async def _acquire_all(
    limiter: HostRateLimiter, clock: ScaledClock, host: str, n: int
) -> list[float]:
    stamps: list[float] = []

    async def hit() -> None:
        await limiter.acquire(host)
        stamps.append(clock())

    await asyncio.gather(*(hit() for _ in range(n)))
    return sorted(stamps)


def _assert_spaced(stamps: list[float], floor: float = MIN_SHARED_API_DELAY_SECONDS) -> None:
    for earlier, later in zip(stamps, stamps[1:], strict=False):
        gap = later - earlier
        assert gap >= floor * _FLOOR_TOLERANCE, (
            f"two requests were {gap:.3f} limiter-seconds apart, under the "
            f"{floor}s floor — concurrent callers are slipping past it"
        )


@pytest.mark.asyncio
async def test_concurrent_callers_on_one_host_are_spaced_by_the_floor() -> None:
    """Four coroutines, one host: each must still be a floor apart.

    Asserts the acquisition *times*, not merely that some waiting happened. A
    coroutine that slept and then fired alongside three others has waited and
    still breached the floor, and only the timestamps tell those apart.
    """
    limiter, clock = _limiter()

    stamps = await _acquire_all(limiter, clock, SHARED, 4)

    assert len(stamps) == 4
    _assert_spaced(stamps)


@pytest.mark.asyncio
async def test_different_hosts_do_not_wait_on_each_other() -> None:
    """Separate hosts keep separate floors — the property the speedup rests on.

    If these serialised, probing four ATS APIs concurrently would cost the sum
    of their floors and `resolve_one`'s fan-out would buy nothing.
    """
    limiter, clock = _limiter()
    stamps: dict[str, float] = {}

    async def hit(host: str) -> None:
        await limiter.acquire(host)
        stamps[host] = clock()

    await asyncio.gather(hit(SHARED), hit(OTHER_SHARED))

    # Both go immediately; neither has a prior request to wait behind.
    assert stamps[SHARED] < MIN_SHARED_API_DELAY_SECONDS
    assert stamps[OTHER_SHARED] < MIN_SHARED_API_DELAY_SECONDS, (
        "a different host was made to wait — the floor is being applied globally"
    )


@pytest.mark.asyncio
async def test_a_penalty_still_holds_under_concurrency() -> None:
    """A 429 backs the host off for everyone, not just the coroutine that saw it."""
    limiter, clock = _limiter()
    limiter.penalize(SHARED, 30.0)

    stamps = await _acquire_all(limiter, clock, SHARED, 3)

    assert min(stamps) >= 30.0 * _FLOOR_TOLERANCE, "a concurrent caller slipped past the penalty"
    _assert_spaced(stamps)


@pytest.mark.asyncio
async def test_many_concurrent_callers_are_all_spaced() -> None:
    """Scaled up, because the real sweep runs far more than four at once."""
    limiter, clock = _limiter()

    stamps = await _acquire_all(limiter, clock, SHARED, 10)

    assert len(stamps) == 10
    _assert_spaced(stamps)
    assert stamps[-1] - stamps[0] >= MIN_SHARED_API_DELAY_SECONDS * 9 * _FLOOR_TOLERANCE
