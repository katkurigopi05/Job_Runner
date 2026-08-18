"""The only way the crawler touches the network.

Every request passes both gates — robots.txt and the per-host floor — because
they are enforced here rather than at each call site. A new extractor cannot
forget to be polite; it has no way to reach the network that skips this.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx
import structlog

from packages.crawler.ratelimit import MIN_DELAY_SECONDS, HostRateLimiter
from packages.crawler.robots import USER_AGENT, RobotsCache

log = structlog.get_logger(__name__)

#: Used when a 429 arrives with no Retry-After to say how long to wait.
_DEFAULT_BACKOFF = 60.0


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

    def __post_init__(self) -> None:
        if self.rate_limiter is None:
            self.rate_limiter = HostRateLimiter()
        if self.robots is None:
            self.robots = RobotsCache(user_agent=self.user_agent, transport=self.transport)

    async def fetch(self, url: str) -> FetchResult:
        """Fetch `url`, waiting as long as politeness requires.

        Raises:
            Blocked: robots.txt says no, or could not be read.
        """
        assert self.robots is not None and self.rate_limiter is not None
        host = urlparse(url).netloc

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

        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
        ) as client:
            response = await client.get(url)

        # Being rate-limited is the server telling us our pace is wrong, and
        # it outranks whatever we had configured. This is the half that makes
        # the faster shared-API floor defensible rather than merely faster.
        if response.status_code in (429, 503):
            self.rate_limiter.penalize(host, _retry_after(response, default=_DEFAULT_BACKOFF))

        return FetchResult(
            url=url,
            status=response.status_code,
            text=response.text,
            content_hash=content_hash(response.text),
            waited=waited,
        )


def build_fetcher(delay_seconds: float | None = None, **kwargs: object) -> PoliteFetcher:
    """Construct a fetcher from settings, refusing an unsafe delay."""
    from packages.core.config import get_settings

    configured = delay_seconds
    if configured is None:
        configured = float(get_settings().crawler_min_delay_s)

    # HostRateLimiter raises rather than clamps, which is the point.
    limiter = HostRateLimiter(delay_seconds=max(configured, MIN_DELAY_SECONDS))
    if configured < MIN_DELAY_SECONDS:
        log.warning(
            "configured_delay_below_floor",
            configured=configured,
            using=MIN_DELAY_SECONDS,
        )
    return PoliteFetcher(rate_limiter=limiter, **kwargs)  # type: ignore[arg-type]
