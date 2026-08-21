"""Staying inside a provider's rate limit.

The first real Gemini run returned `429 Too Many Requests` on **39 of 60**
calls, and the evaluation reported that as tailoring failure — a dead API and
a model with poor judgement looked identical. These pin the pacing that keeps
us under the limit and the backoff that obeys the provider when we still go
over.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from packages.llm import pacing
from packages.llm.pacing import (
    MAX_WAIT_S,
    ProviderPacer,
    pacer_for,
    retry_after_seconds,
)


@pytest.mark.asyncio
async def test_calls_are_spaced_out() -> None:
    pacer = ProviderPacer(interval_s=0.05)

    start = time.monotonic()
    await pacer.wait_turn()
    await pacer.wait_turn()

    assert time.monotonic() - start >= 0.05


@pytest.mark.asyncio
async def test_concurrent_callers_do_not_both_go_at_once() -> None:
    """Two passes that each read the clock and decide "now" would both go now.

    That burst is exactly what the limit refuses, so the check and the update
    have to happen under one lock.
    """
    pacer = ProviderPacer(interval_s=0.05)

    start = time.monotonic()
    await asyncio.gather(*(pacer.wait_turn() for _ in range(3)))

    assert time.monotonic() - start >= 0.10


def test_retry_after_is_read_when_present() -> None:
    assert retry_after_seconds({"retry-after": "30"}) == 30.0
    assert retry_after_seconds({"Retry-After": "7.5"}) == 7.5


def test_an_unparseable_retry_after_is_ignored_rather_than_guessed() -> None:
    """The HTTP-date form is legal and rare here.

    Parsing it wrong yields a wait of either zero — which hammers the provider
    — or days. Falling back to the doubling backoff is the safe reading.
    """
    assert retry_after_seconds({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}) is None
    assert retry_after_seconds({}) is None
    assert retry_after_seconds(None) is None


@pytest.mark.asyncio
async def test_a_long_retry_after_is_capped(monkeypatch) -> None:
    """A provider asking for an hour should surface, not hang the job.

    The sleep is intercepted rather than performed — a test that waits out the
    cap to prove the cap exists takes as long as the thing it is guarding
    against.
    """
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(pacing.asyncio, "sleep", fake_sleep)
    pacer = ProviderPacer(interval_s=0.0)

    await pacer.back_off(0, retry_after=MAX_WAIT_S + 3600)

    assert slept == [MAX_WAIT_S]


@pytest.mark.asyncio
async def test_backoff_doubles_when_the_provider_says_nothing(monkeypatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(pacing.asyncio, "sleep", fake_sleep)
    pacer = ProviderPacer(interval_s=0.0)

    await pacer.back_off(0, retry_after=None)
    await pacer.back_off(1, retry_after=None)

    assert slept[1] > slept[0]


def test_the_pacer_is_shared_per_provider() -> None:
    """The limit belongs to the account, not to one provider object.

    A fresh instance per request would reset the spacing every call, which is
    the same as having no pacing at all.
    """
    assert pacer_for("gemini") is pacer_for("gemini")
    assert pacer_for("gemini") is not pacer_for("anthropic")
