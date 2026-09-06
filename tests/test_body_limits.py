"""An unknown host must not be able to spend all the memory — §2.6 in spirit.

The bespoke sweep points the fetcher at thousands of hosts nobody has vetted.
Neither the page body nor robots.txt had a size limit, so one of them serving
an endless response took the worker with it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from packages.crawler.fetch import MAX_BODY_BYTES, PoliteFetcher, TooLarge
from packages.crawler.ratelimit import HostRateLimiter
from packages.crawler.robots import ROBOTS_PARSE_LIMIT_BYTES, RobotsCache

ALLOW_ALL = "User-agent: *\nAllow: /\n"
_CHUNK = 1024 * 1024


def _fetcher(handler) -> PoliteFetcher:
    return PoliteFetcher(
        transport=httpx.MockTransport(handler),
        rate_limiter=HostRateLimiter(clock=lambda: 0.0),
    )


@pytest.mark.asyncio
async def test_an_endless_body_is_abandoned_rather_than_read() -> None:
    """The host never stops sending. Without a cap, neither do we.

    Counting what the far end produced is the assertion that matters: a limit
    checked after `response.text` raises just as loudly and buys nothing,
    because the memory is already spent by then.
    """
    produced = {"bytes": 0}

    async def endless() -> AsyncIterator[bytes]:
        while True:
            produced["bytes"] += _CHUNK
            yield b"x" * _CHUNK

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        return httpx.Response(200, content=endless())

    with pytest.raises(TooLarge):
        await asyncio.wait_for(_fetcher(handler).fetch("https://endless.example/x"), timeout=60)

    assert produced["bytes"] < MAX_BODY_BYTES * 2, "kept reading well past the limit"


@pytest.mark.asyncio
async def test_an_ordinary_body_is_untouched() -> None:
    """A real board is ~5 KB a posting. The cap must be invisible to it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        return httpx.Response(200, text="a normal board response")

    result = await _fetcher(handler).fetch("https://acme.example/jobs")

    assert result.status == 200
    assert result.text == "a normal board response"
    assert result.content_hash


@pytest.mark.asyncio
async def test_a_429_still_backs_the_host_off() -> None:
    """Streaming moved this code, and nothing was asserting it.

    §2.6 permits the 2s shared floor only "while also listening", so losing the
    backoff would quietly remove the half that makes the faster floor
    defensible. It went missing during the rewrite and no test noticed.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        return httpx.Response(429, headers={"Retry-After": "300"}, text="slow down")

    fetcher = _fetcher(handler)
    result = await fetcher.fetch("https://busy.example/jobs")

    assert result.status == 429
    assert fetcher.rate_limiter is not None
    assert fetcher.rate_limiter.time_until_ready("busy.example") == pytest.approx(300.0)


@pytest.mark.asyncio
async def test_a_huge_robots_file_is_parsed_up_to_the_limit() -> None:
    """RFC 9309 §2.5: impose a parsing limit, and it MUST be >= 500 KiB.

    Rules before the limit are still obeyed. Rules after it are not — that is
    the spec's own trade, and `robots_truncated` is logged whenever it applies.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        early = "User-agent: *\nDisallow: /private\n" + "#pad\n" * 200_000
        return httpx.Response(200, text=early)

    cache = RobotsCache(transport=httpx.MockTransport(handler))

    assert (await cache.check("https://huge.example/public")).allowed
    assert not (await cache.check("https://huge.example/private")).allowed


def test_the_two_limits_are_not_the_same_number() -> None:
    """Different questions. The robots one is the RFC's floor; the body one is
    a policy default sized from a measured board (~5 KB a posting)."""
    assert ROBOTS_PARSE_LIMIT_BYTES == 500 * 1024
    assert MAX_BODY_BYTES > ROBOTS_PARSE_LIMIT_BYTES
