"""A daily ceiling on calls that leave the machine, and it fails closed.

Cost is not the exposure here — §3 mandates free tiers and §11 bars paid APIs.
**Quota** is. Gemini's free tier is request-capped per day, and one tailoring
pass across a full match feed can spend the day's allowance in a few minutes.
`max_tokens` in `provider.py` bounds a single call and nothing bounds the set.

## Why it fails closed

The tempting behaviour when the budget runs out is to fall back to a local
model and carry on. That would put two different quality tiers inside one
résumé with nothing recording where the seam is, and the owner would send it
believing it was tailored. Refusing is worse in the moment and better
afterwards: the application parks, the reason is visible, and it resumes
tomorrow or after the limit is raised.

Local providers are never limited. Nothing leaves the machine, so there is no
allowance to spend.

## Where the count comes from

The audit trail, which already records every call with a timestamp and a
provider. Deriving from it means there is one authority rather than a counter
that can disagree with the record — and a counter that disagrees with the
audit trail is a worse problem than the one it solves.

That makes the check O(trail). At one user's volume the trail is small; if it
ever is not, the fix is to roll the file, not to keep a second tally.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from packages.core.config import get_settings
from packages.llm.audit import LOCAL_PROVIDERS, read_trail

log = structlog.get_logger(__name__)


class QuotaExceeded(RuntimeError):
    """The daily allowance for a remote provider is spent."""

    def __init__(self, provider: str, used: int, limit: int) -> None:
        self.provider = provider
        self.used = used
        self.limit = limit
        super().__init__(
            f"{provider} has used {used} of {limit} calls allowed today. "
            "Raise LLM_DAILY_REMOTE_CALLS, wait for the reset, or run a local "
            "provider — this refuses rather than silently downgrading."
        )


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def used_today(provider: str) -> int:
    """Calls to `provider` recorded in the trail so far today (UTC)."""
    today = _today()
    return sum(
        1 for entry in read_trail() if entry.provider == provider and entry.at.startswith(today)
    )


def limit_for(provider: str) -> int | None:
    """The daily ceiling, or None when the provider is unlimited."""
    if provider in LOCAL_PROVIDERS:
        return None
    limit = get_settings().llm_daily_remote_calls
    return limit if limit > 0 else None


def authorize(provider: str) -> None:
    """Permit one call to `provider`, or refuse it.

    Raises:
        QuotaExceeded: the allowance is spent. Deliberate — see the module
            docstring on why this does not fall back.
    """
    limit = limit_for(provider)
    if limit is None:
        return

    used = used_today(provider)
    if used >= limit:
        log.warning("llm_quota_exceeded", provider=provider, used=used, limit=limit)
        raise QuotaExceeded(provider, used, limit)

    if used + 1 == limit:
        log.warning("llm_quota_last_call", provider=provider, limit=limit)


def remaining(provider: str) -> int | None:
    """Calls left today, or None when unlimited. For the dashboard."""
    limit = limit_for(provider)
    return None if limit is None else max(0, limit - used_today(provider))
