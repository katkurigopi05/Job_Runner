"""The only way the crawler touches the network.

Every request passes both gates — robots.txt and the per-host floor — because
they are enforced here rather than at each call site. A new extractor cannot
forget to be polite; it has no way to reach the network that skips this.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
import structlog

from packages.crawler.ratelimit import MIN_DELAY_SECONDS, HostRateLimiter, host_key
from packages.crawler.robots import USER_AGENT, RobotsCache

log = structlog.get_logger(__name__)

#: Used when a 429 arrives with no Retry-After to say how long to wait.
_DEFAULT_BACKOFF = 60.0

#: The largest body this will read. Not a spec number like the robots limit —
#: a policy one, so here is the measurement behind it. A real Greenhouse board
#: with `content=true` runs about 5 KB per posting (the golden fixture is 62 KB
#: for 12), so even a employer with 5,000 open roles lands near 25 MB. 64 gives
#: that better than twice over while still stopping a host that never ends.
#:
#: Exceeding it raises rather than truncating. A short read of a board is
#: invalid JSON at best and fewer postings at worst, and "fewer postings"
#: reads exactly like "nothing new since the last poll" — the failure this
#: repo has already been bitten by twice. Refusing is loud; truncating is not.
MAX_BODY_BYTES = 64 * 1024 * 1024


def _retry_after(response: httpx.Response, *, default: float) -> float:
    """Seconds the server asked us to wait, or `default` if it did not say.

    `Retry-After` comes as either a delay in seconds or an HTTP date; both
    are in the spec and both appear in the wild.
    """
    raw = response.headers.get("Retry-After")
    if not raw:
        return default
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError):
        return default
    if when is None:
        return default
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class Blocked(Exception):
    """robots.txt disallows this URL, or its rules could not be read."""


class TooLarge(Exception):
    """The body passed `MAX_BODY_BYTES` and was abandoned unread.

    Deliberately not a `Blocked`. Being told no is a normal outcome the sweeps
    report as such; this is a host misbehaving, and the callers' generic
    handlers already treat it the right way — one company fails, the cycle
    goes on.
    """


@dataclass
class FetchResult:
    url: str
    status: int
    text: str
    #: sha256 of the body — the change-detection key.
    content_hash: str
    #: Seconds spent waiting on the rate limiter.
    waited: float = 0.0

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class PoliteFetcher:
    """An HTTP client that cannot outrun the rules."""

    rate_limiter: HostRateLimiter | None = None
    robots: RobotsCache | None = None
    user_agent: str = USER_AGENT
    transport: httpx.AsyncBaseTransport | None = None
    timeout: float = 30.0
    #: Connection pool size. `None` means httpx's own defaults, which is what
    #: a directly-constructed fetcher gets; `build_fetcher` fills these from
    #: settings.
    max_connections: int | None = None
    max_keepalive: int | None = None
    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.rate_limiter is None:
            self.rate_limiter = HostRateLimiter()
        if self.robots is None:
            self.robots = RobotsCache(user_agent=self.user_agent, transport=self.transport)

    def _client_for_requests(self) -> httpx.AsyncClient:
        """The pooled client, built on first use.

        Lazily, because a fetcher is constructed in ordinary synchronous code
        — `build_fetcher()` at the top of a handler — and an `AsyncClient`
        binds to the event loop that first uses it. Building it in
        `__post_init__` would tie the object to whichever loop happened to be
        running at construction, which is how a fetcher built once and used by
        two cycles fails with an error about a different loop.

        Rebuilt if it has been closed, so `aclose()` is not a one-way door
        for a fetcher someone reuses afterwards.
        """
        if self._client is None or self._client.is_closed:
            limits = None
            if self.max_connections is not None or self.max_keepalive is not None:
                limits = httpx.Limits(
                    max_connections=self.max_connections,
                    max_keepalive_connections=self.max_keepalive,
                )
            self._client = httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout,
                headers={"User-Agent": self.user_agent},
                # Redirects are followed by hand, one hop at a time, so each
                # hop goes through both gates. See `_walk`.
                follow_redirects=False,
                **({"limits": limits} if limits is not None else {}),
            )
        return self._client

    async def aclose(self) -> None:
        """Release the pool, and the robots cache's with it."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
        if self.robots is not None:
            await self.robots.aclose()

    async def __aenter__(self) -> PoliteFetcher:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    #: How many hops a redirect chain may take before we call it a loop. httpx
    #: defaults to 20; a careers page that needs more than five is broken.
    _MAX_REDIRECTS = 5

    async def _walk(
        self,
        client: httpx.AsyncClient,
        url: str,
        host: str,
    ) -> tuple[int, str, str]:
        """Follow the chain and read the final body. Returns (status, text, host).

        Streamed, one hop at a time, for two reasons that used to be handled in
        different places and both belong here.

        **Every hop goes through both gates.** `follow_redirects=True` did this
        inside httpx, which knows nothing about robots.txt: a site answering 302
        to another host had that host fetched with no robots check and no rate
        limit of its own, so `first.example` could hand us a page from
        `second.example` whose robots.txt says `Disallow: /` — never consulted,
        never even asked. That made §2.6 advisory rather than enforced.

        **The body is capped while it arrives.** `client.get` buffers the whole
        response before returning, so a limit applied to `response.text` raises
        after the memory is already gone — measured at 200 MB allocated for a
        200 MB body before the check ran. Streaming is what makes the limit a
        limit.

        The host is returned because the caller records a 429 against whichever
        machine actually sent it.
        """
        for _ in range(self._MAX_REDIRECTS + 1):
            async with client.stream("GET", url) as response:
                if not response.is_redirect:
                    if response.status_code in (429, 503):
                        # Being rate-limited is the server telling us our pace
                        # is wrong, and it outranks whatever we had configured.
                        # This is the half that makes the faster shared-API
                        # floor defensible rather than merely faster, so it is
                        # recorded against the host that actually sent it.
                        self.rate_limiter.penalize(  # type: ignore[union-attr]
                            host, _retry_after(response, default=_DEFAULT_BACKOFF)
                        )
                        # The body is not read: nothing in it is worth having,
                        # and reading it would delay the backoff just asked for.
                        return response.status_code, "", host
                    return response.status_code, await self._read_body(response, url), host

                target = response.headers.get("Location")
                if not target:
                    return response.status_code, "", host
                # Relative Locations are legal and common; resolve against the
                # URL that issued them rather than guessing.
                next_url = str(response.url.join(target))
                next_host = host_key(next_url)

            if next_host != host:
                # A new machine. It gets its own robots verdict and its own
                # place in the queue, exactly as if we had started here.
                decision = await self.robots.check(next_url)  # type: ignore[union-attr]
                if not decision.allowed:
                    raise Blocked(f"{next_url}: {decision.reason} (redirected from {host})")
                await self.rate_limiter.acquire(next_host)  # type: ignore[union-attr]
                log.info("followed_cross_host_redirect", frm=host, to=next_host)
                host = next_host
            else:
                # Same machine, so no second robots fetch and no second wait —
                # the hop is part of one logical request. It still cost the
                # server a round trip, so it is recorded.
                self.rate_limiter.record(host)  # type: ignore[union-attr]

            url = next_url

        raise Blocked(f"{url}: more than {self._MAX_REDIRECTS} redirects")

    async def fetch(self, url: str) -> FetchResult:
        """Fetch `url`, waiting as long as politeness requires.

        Raises:
            Blocked: robots.txt says no, or could not be read.
        """
        assert self.robots is not None and self.rate_limiter is not None
        host = host_key(url)

        decision = await self.robots.check(url)
        if not decision.allowed:
            raise Blocked(f"{url}: {decision.reason}")

        # A site asking for more space than our floor gets it. Asking for less
        # changes nothing — §2.6 is configurable upward only.
        #
        # Recorded against *this host*. Writing it to `delay_seconds` would
        # make one slow site's crawl-delay the delay for every other site in
        # the registry, which is not what that site asked for.
        current = self.rate_limiter.delay_for(host)
        if decision.crawl_delay and decision.crawl_delay > current:
            log.info(
                "honouring_site_crawl_delay",
                host=host,
                site_delay=decision.crawl_delay,
                our_delay=current,
            )
            self.rate_limiter.host_delays[host] = float(decision.crawl_delay)

        waited = await self.rate_limiter.acquire(host)

        # Both gates are behind us. Reusing the connection from here changes
        # how much setup is repeated, never how long anything waited.
        status, text, host = await self._walk(self._client_for_requests(), url, host)

        return FetchResult(
            url=url,
            status=status,
            text=text,
            content_hash=content_hash(text),
            waited=waited,
        )

    async def _read_body(self, response: httpx.Response, url: str) -> str:
        """The body, or `TooLarge` before it can exhaust the worker.

        `httpx` has no size option and `response.text` has already bought the
        whole thing, so the check has to happen while the bytes arrive. The
        bespoke sweep points this at thousands of hosts nobody has vetted,
        which is where an unbounded read stops being theoretical.
        """
        total = 0
        chunks: list[bytes] = []
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                log.warning("body_too_large", url=url, limit=MAX_BODY_BYTES)
                raise TooLarge(f"{url}: body exceeds {MAX_BODY_BYTES} bytes")
            chunks.append(chunk)
        return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")


def build_fetcher(delay_seconds: float | None = None, **kwargs: object) -> PoliteFetcher:
    """Construct a fetcher from settings, refusing an unsafe delay."""
    from packages.core.config import get_settings

    configured = delay_seconds
    if configured is None:
        configured = float(get_settings().crawler_min_delay_s)

    settings = get_settings()

    # HostRateLimiter raises rather than clamps, which is the point.
    limiter = HostRateLimiter(delay_seconds=max(configured, MIN_DELAY_SECONDS))
    if configured < MIN_DELAY_SECONDS:
        log.warning(
            "configured_delay_below_floor",
            configured=configured,
            using=MIN_DELAY_SECONDS,
        )
    # Only supplied when the caller has not; a test handing in its own pool
    # size or timeout keeps it.
    kwargs.setdefault("max_connections", settings.crawler_http_max_connections)
    kwargs.setdefault("max_keepalive", settings.crawler_http_max_keepalive)
    kwargs.setdefault("timeout", settings.crawler_http_timeout_s)
    return PoliteFetcher(rate_limiter=limiter, **kwargs)  # type: ignore[arg-type]
