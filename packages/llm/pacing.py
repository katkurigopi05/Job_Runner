"""Keep remote LLM calls inside the provider's rate limit.

The tailorer makes one call per bullet, so a single résumé is five calls fired
back to back and a batch is hundreds. Gemini's free tier is limited per minute
as well as per day, and the per-minute one is what a loop like that trips —
`429 Too Many Requests` on **39 of 60** calls in the first real run, which the
evaluation then reported as tailoring failures.

This is the same discipline `packages/crawler/ratelimit.py` applies to job
boards, for the same reason: a service telling us to slow down is information,
not an obstacle. Two rules, and the second is the one that matters.

**Space calls out.** A minimum interval between requests to a given provider,
so the limit is usually never reached rather than repeatedly hit and backed
off from.

**Obey `Retry-After`.** When the provider does say no, wait exactly as long as
it asked and try again, up to a bounded number of attempts. Guessing a shorter
delay is how a client turns one 429 into a stream of them.

What this deliberately does **not** do is spread work across several API keys
to obtain more quota than one account is granted. That is circumventing the
limit rather than respecting it, and it belongs in the same category as the
captcha evasion CLAUDE.md §2.5 rules out. Pacing is the supported answer; when
real usage genuinely outgrows a free tier, the honest fix is a paid tier and
the owner's decision, not more keys.

Local providers are never paced. Nothing leaves the machine, so there is no
allowance to respect.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)

#: Minimum seconds between calls to one remote provider. Sized for the
#: slowest free tier this project targets rather than the fastest, because the
#: cost of being too slow is a longer batch and the cost of being too fast is
#: a run that fails most of the way through.
DEFAULT_INTERVAL_S = 4.0

#: How many times a single call may be retried after a 429. Bounded so a
#: provider having a bad day fails the run rather than hanging it.
MAX_RETRIES = 3

#: Used when a 429 carries no `Retry-After`. Doubles per attempt.
FALLBACK_BACKOFF_S = 20.0

#: A ceiling on any single wait, so a provider asking for an hour surfaces as
#: an error the owner can act on rather than a job that appears to hang.
MAX_WAIT_S = 120.0


class RateLimited(Exception):
    """The provider refused, and retrying within the bounds did not help."""


@dataclass
class ProviderPacer:
    """Spaces calls to one provider and backs off when it objects."""

    interval_s: float = DEFAULT_INTERVAL_S
    _last_call: float = field(default=0.0, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def wait_turn(self) -> float:
        """Block until enough time has passed since the previous call.

        Serialized by a lock: two concurrent tailoring passes that each check
        the clock and decide they may go now would both go now, which is the
        burst this exists to prevent.
        """
        async with self._lock:
            elapsed = time.monotonic() - self._last_call
            delay = self.interval_s - elapsed
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_call = time.monotonic()
            return max(delay, 0.0)

    async def back_off(self, attempt: int, retry_after: float | None) -> None:
        """Wait after a refusal — as long as asked, or a doubling fallback."""
        wait = retry_after if retry_after is not None else FALLBACK_BACKOFF_S * (2**attempt)
        wait = min(wait, MAX_WAIT_S)
        log.info("llm_rate_limited", attempt=attempt + 1, waiting_s=round(wait, 1))
        await asyncio.sleep(wait)
        # The next call starts its interval from now, not from the last
        # successful call, or the retry lands immediately after the backoff.
        self._last_call = time.monotonic()


#: One pacer per provider name. Module level because the limit belongs to the
#: account, not to whichever object happens to be making the call — a fresh
#: provider instance per request would otherwise reset the spacing every time.
_PACERS: dict[str, ProviderPacer] = {}


def configured_interval() -> float:
    """The spacing this machine is set to use.

    Read on every call rather than captured once: the pacer is module-level
    and outlives any single settings object, so a cached value would ignore a
    change — including the zero the test suite sets, where these sleeps are
    pure wall clock and would add minutes to a run.
    """
    from packages.core.config import get_settings

    return max(float(get_settings().llm_call_interval_s), 0.0)


def pacer_for(provider: str, *, interval_s: float | None = None) -> ProviderPacer:
    interval = interval_s if interval_s is not None else configured_interval()
    existing = _PACERS.get(provider)
    if existing is None:
        existing = ProviderPacer(interval_s=interval)
        _PACERS[provider] = existing
    else:
        existing.interval_s = interval
    return existing


def retry_after_seconds(headers: object) -> float | None:
    """`Retry-After` in seconds, when the provider sent one.

    Only the delta-seconds form is read. The HTTP-date form is legal and rare
    here, and parsing it wrong would produce a wait of either zero or days.
    """
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    raw = getter("retry-after") or getter("Retry-After")
    if not raw:
        return None
    try:
        seconds = float(str(raw).strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None
