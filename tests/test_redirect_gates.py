"""A redirect is a request, so it goes through both gates — §2.6.

`follow_redirects=True` handed the chain to httpx, which knows nothing about
robots.txt. Every hop after the first was unpoliced.
"""

from __future__ import annotations

import httpx
import pytest

from packages.crawler.fetch import Blocked, PoliteFetcher
from packages.crawler.ratelimit import HostRateLimiter

ALLOW_ALL = "User-agent: *\nAllow: /\n"
DISALLOW_ALL = "User-agent: *\nDisallow: /\n"


def _fetcher(handler) -> PoliteFetcher:
    return PoliteFetcher(
        transport=httpx.MockTransport(handler),
        rate_limiter=HostRateLimiter(clock=lambda: 0.0),
    )


@pytest.mark.asyncio
async def test_a_redirect_cannot_smuggle_us_into_a_disallowed_host() -> None:
    """The whole point. Any company in the registry could 302 anywhere.

    `first.example` is allowed and redirects to `second.example`, which
    forbids everything. Before this, the disallowed body came back 200 and
    that host's robots.txt was never requested.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            body = DISALLOW_ALL if request.url.host == "second.example" else ALLOW_ALL
            return httpx.Response(200, text=body)
        if request.url.host == "first.example":
            return httpx.Response(302, headers={"Location": "https://second.example/secret"})
        return httpx.Response(200, text="CONTENT OF A DISALLOWED PAGE")

    with pytest.raises(Blocked) as caught:
        await _fetcher(handler).fetch("https://first.example/start")

    assert "second.example" in str(caught.value)
    assert "https://second.example/robots.txt" in seen, "the new host must be asked"
    assert not any(u.endswith("/secret") for u in seen), "the disallowed page was fetched"


@pytest.mark.asyncio
async def test_a_same_host_redirect_still_resolves() -> None:
    """The common case — `/old` to `/new`, or a trailing slash — must not break."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "/new"})
        return httpx.Response(200, text="the real page")

    result = await _fetcher(handler).fetch("https://acme.example/old")

    assert result.status == 200
    assert result.text == "the real page"


@pytest.mark.asyncio
async def test_a_cross_host_redirect_to_an_allowed_host_is_followed() -> None:
    """Blocking every cross-host hop would break `acme.com` -> `careers.acme.com`.

    The rule is that the new host is *asked*, not that it is refused.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        if request.url.host == "a.example":
            return httpx.Response(302, headers={"Location": "https://b.example/page"})
        return httpx.Response(200, text="allowed target")

    result = await _fetcher(handler).fetch("https://a.example/x")

    assert result.text == "allowed target"
    assert "https://b.example/robots.txt" in seen


@pytest.mark.asyncio
async def test_a_redirect_loop_ends() -> None:
    """Following by hand means the hop cap is ours to enforce."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        return httpx.Response(302, headers={"Location": "https://loop.example/again"})

    with pytest.raises(Blocked, match="redirects"):
        await _fetcher(handler).fetch("https://loop.example/start")
