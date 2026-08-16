"""Per-host rate limiting — CLAUDE.md §2.6.

"Minimum 60s between requests to the same host. Configurable up, never down."

That is enforced structurally rather than by convention: `HostRateLimiter`
raises on a delay below the floor instead of quietly clamping. A silent clamp
would let a config change *look* like it took effect while the crawler kept
hammering someone's careers page, and the person who finds out is the site
owner, not us.

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

#: Bounds `acquire`, so a non-advancing clock fails loudly instead of hanging.
_MAX_WAIT_ROUNDS = 100


class RateLimitTooLow(ValueError):
    """A configured delay below the floor. Refused, never clamped."""

    def __init__(self, requested: float) -> None:
        super().__init__(
            f"{requested}s between requests is below the {MIN_DELAY_SECONDS}s floor. "
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
    _last_request: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.delay_seconds < MIN_DELAY_SECONDS:
            raise RateLimitTooLow(self.delay_seconds)

    def time_until_ready(self, host: str) -> float:
        """Seconds a caller must wait before touching `host`. 0.0 if ready."""
        last = self._last_request.get(host)
        if last is None:
            return 0.0
        elapsed = self.clock() - last
        return max(0.0, self.delay_seconds - elapsed)

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
        else:
            self._last_request.pop(host, None)
