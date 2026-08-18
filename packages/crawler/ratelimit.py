"""Per-host rate limiting — CLAUDE.md §2.6.

"Minimum 60s between requests to the same host. Configurable up, never down."

That is enforced structurally rather than by convention: `HostRateLimiter`
raises on a delay below the floor instead of quietly clamping. A silent clamp
would let a config change *look* like it took effect while the crawler kept
hammering someone's careers page, and the person who finds out is the site
owner, not us.

## Two kinds of host

§2.6 was written with one picture in mind: a company's own careers page,
served by that company's own infrastructure, where 60 seconds is courteous and
anything faster is not. That picture is still right, and it is still the
default.

It is the wrong picture for a *multi-tenant ATS API*. Every Greenhouse board
on earth answers from `boards-api.greenhouse.io`; Lever and Ashby are the
same. Keying on host there does not spread load across the companies being
polled — it serializes all of them behind one counter, so the crawler's whole
reach becomes 60 boards an hour no matter how many companies are listed. The
limit stops protecting anyone and starts capping coverage instead.

So a host that is *known* to be a shared ATS API gets its own floor,
`MIN_SHARED_API_DELAY_SECONDS`. The rules that make this safe rather than a
loophole:

- The list is explicit. An unknown host is a company host and gets 60s. There
  is no heuristic that could promote one by accident.
- The shared floor is a floor too. It is refused below, never clamped, exactly
  like the main one.
- `penalize()` exists and is used. A 429 or a `Retry-After` backs that host off
  for as long as it asked, which is the actual contract these APIs publish.
  Polling faster is only defensible while you are also listening.

The clock is injectable so tests can prove the waiting without waiting.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)

#: The floor from §2.6. Not a default — a hard minimum.
MIN_DELAY_SECONDS = 60.0

#: The floor for a shared ATS API. Lower than §2.6's, and still a floor.
#: Two seconds is well inside what these APIs serve without complaint, and
#: `penalize()` is what handles the case where one disagrees.
MIN_SHARED_API_DELAY_SECONDS = 2.0

#: Multi-tenant ATS hosts: one endpoint serving thousands of companies'
#: boards. Explicit by design — an unknown host is a company host.
SHARED_API_HOSTS: frozenset[str] = frozenset(
    {
        "boards-api.greenhouse.io",
        "api.lever.co",
        "api.ashbyhq.com",
    }
)

#: Bounds `acquire`, so a non-advancing clock fails loudly instead of hanging.
_MAX_WAIT_ROUNDS = 100


def floor_for(host: str) -> float:
    """The minimum delay this host may be polled at."""
    return MIN_SHARED_API_DELAY_SECONDS if host in SHARED_API_HOSTS else MIN_DELAY_SECONDS


class RateLimitTooLow(ValueError):
    """A configured delay below the floor. Refused, never clamped."""

    def __init__(self, requested: float, floor: float = MIN_DELAY_SECONDS) -> None:
        super().__init__(
            f"{requested}s between requests is below the {floor}s floor. "
            "This limit is configurable upward only (CLAUDE.md §2.6)."
        )


@dataclass
class HostRateLimiter:
    """Tracks the last request per host and makes callers wait their turn."""

    delay_seconds: float = MIN_DELAY_SECONDS
    #: Injectable for tests; defaults to the monotonic clock.
    clock: Callable[[], float] = time.monotonic
    #: Also injectable, and paired with `clock` — a fake sleeper is expected to
    #: advance the fake clock. Injecting rather than patching `asyncio.sleep`
    #: keeps the substitution local to one limiter instead of process-wide.
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep
    #: Per-host overrides. A host absent here uses `delay_seconds`, subject
    #: to its own floor. Refused below the floor, like everything else.
    host_delays: dict[str, float] = field(default_factory=dict)
    _last_request: dict[str, float] = field(default_factory=dict)
    #: Host -> clock time before which it must not be touched, set by a 429
    #: or a Retry-After. Outranks the ordinary delay; never shortens it.
    _blocked_until: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.delay_seconds < MIN_DELAY_SECONDS:
            raise RateLimitTooLow(self.delay_seconds)
        for host, delay in self.host_delays.items():
            floor = floor_for(host)
            if delay < floor:
                raise RateLimitTooLow(delay, floor)

    def delay_for(self, host: str) -> float:
        """The delay actually applied to `host`.

        A shared ATS API uses its own floor unless overridden upward. A
        company host uses `delay_seconds`, which can never be below 60s.
        """
        if host in self.host_delays:
            return self.host_delays[host]
        if host in SHARED_API_HOSTS:
            return MIN_SHARED_API_DELAY_SECONDS
        return self.delay_seconds

    def penalize(self, host: str, seconds: float) -> None:
        """Back off `host` for `seconds` — a 429, or a Retry-After header.

        Only ever extends. A server asking for a longer pause than we planned
        gets it; one asking for a shorter pause does not shorten ours.
        """
        if seconds <= 0:
            return
        until = self.clock() + seconds
        self._blocked_until[host] = max(self._blocked_until.get(host, 0.0), until)
        log.info("rate_limit_penalty", host=host, seconds=round(seconds, 1))

    def time_until_ready(self, host: str) -> float:
        """Seconds a caller must wait before touching `host`. 0.0 if ready."""
        now = self.clock()

        penalty = max(0.0, self._blocked_until.get(host, 0.0) - now)

        last = self._last_request.get(host)
        ordinary = 0.0 if last is None else max(0.0, self.delay_for(host) - (now - last))

        return max(penalty, ordinary)

    def is_ready(self, host: str) -> bool:
        return self.time_until_ready(host) <= 0.0

    def record(self, host: str) -> None:
        """Mark a request as just made. Call this even for failed requests.

        A 500 still cost the host a round trip; retrying immediately because
        it did not succeed is exactly the behaviour the floor exists to stop.
        """
        self._last_request[host] = self.clock()

    async def acquire(self, host: str) -> float:
        """Wait until `host` may be requested. Returns how long it waited.

        Loops rather than sleeping once, because a sleep can return early.
        `_MAX_WAIT_ROUNDS` bounds it: with a clock that never advances this
        would otherwise spin forever, which is a hang rather than an error.
        """
        waited = 0.0
        for _ in range(_MAX_WAIT_ROUNDS):
            remaining = self.time_until_ready(host)
            if remaining <= 0:
                break
            log.debug("rate_limit_wait", host=host, seconds=round(remaining, 1))
            await self.sleeper(remaining)
            waited += remaining
        else:
            raise RuntimeError(
                f"rate limiter never became ready for {host}; the clock is not "
                "advancing (in tests, the fake sleeper must advance the fake clock)"
            )
        self.record(host)
        return waited

    def reset(self, host: str | None = None) -> None:
        if host is None:
            self._last_request.clear()
            self._blocked_until.clear()
        else:
            self._last_request.pop(host, None)
            self._blocked_until.pop(host, None)
