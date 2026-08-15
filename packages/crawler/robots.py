"""robots.txt — CLAUDE.md §2.6.

Two decisions worth stating, because both cut against convenience:

- **Unreachable robots.txt means do not crawl.** The permissive reading is
  that an error means no rules, so everything is allowed. This does the
  opposite: if we cannot read the rules, we do not get to assume they favour
  us. A 404 is different — that genuinely means no file exists, which the
  standard treats as unrestricted.
- **Crawl-delay is honoured when it is longer than ours.** A site asking for
  more space gets it. A site asking for less does not, because §2.6's floor
  is configurable upward only.
"""

from __future__ import annotations

import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
import structlog

log = structlog.get_logger(__name__)

USER_AGENT = "jobrunner"

#: Re-read robots.txt at most this often per host.
CACHE_TTL_SECONDS = 3600.0


@dataclass
class RobotsDecision:
    allowed: bool
    reason: str
    #: The site's own Crawl-delay, if it published one.
    crawl_delay: float | None = None


@dataclass
class _CachedRobots:
    parser: urllib.robotparser.RobotFileParser | None
    fetched_at: float
    reachable: bool
    missing: bool = False


@dataclass
class RobotsCache:
    """Fetches and caches robots.txt per host."""

    user_agent: str = USER_AGENT
    ttl_seconds: float = CACHE_TTL_SECONDS
    transport: httpx.AsyncBaseTransport | None = None
    timeout: float = 15.0
    _cache: dict[str, _CachedRobots] = field(default_factory=dict)

    def _fresh(self, host: str) -> _CachedRobots | None:
        entry = self._cache.get(host)
        if entry is None:
            return None
        if time.monotonic() - entry.fetched_at > self.ttl_seconds:
            return None
        return entry

    async def _load(self, url: str) -> _CachedRobots:
        parsed = urlparse(url)
        host = parsed.netloc
        robots_url = urljoin(f"{parsed.scheme}://{host}", "/robots.txt")

        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout,
                headers={"User-Agent": self.user_agent},
                follow_redirects=True,
            ) as client:
                response = await client.get(robots_url)
        except Exception as exc:  # noqa: BLE001 - any failure means "unknown"
            log.warning("robots_unreachable", host=host, error=type(exc).__name__)
            entry = _CachedRobots(parser=None, fetched_at=time.monotonic(), reachable=False)
            self._cache[host] = entry
            return entry

        if response.status_code == 404:
            # No robots.txt is a real answer: the standard reads it as
            # unrestricted.
            entry = _CachedRobots(
                parser=None, fetched_at=time.monotonic(), reachable=True, missing=True
            )
            self._cache[host] = entry
            return entry

        if response.status_code >= 400:
            log.warning("robots_error_status", host=host, status=response.status_code)
            entry = _CachedRobots(parser=None, fetched_at=time.monotonic(), reachable=False)
            self._cache[host] = entry
            return entry

        parser = urllib.robotparser.RobotFileParser()
        parser.parse(response.text.splitlines())
        entry = _CachedRobots(parser=parser, fetched_at=time.monotonic(), reachable=True)
        self._cache[host] = entry
        return entry

    async def check(self, url: str) -> RobotsDecision:
        """Whether `url` may be fetched, and any Crawl-delay the site asks for."""
        host = urlparse(url).netloc
        entry = self._fresh(host) or await self._load(url)

        if not entry.reachable:
            return RobotsDecision(
                allowed=False,
                reason=("robots.txt could not be read; refusing to assume the rules allow us"),
            )

        if entry.missing or entry.parser is None:
            return RobotsDecision(allowed=True, reason="no robots.txt published")

        allowed = entry.parser.can_fetch(self.user_agent, url)
        delay = entry.parser.crawl_delay(self.user_agent)

        return RobotsDecision(
            allowed=allowed,
            reason="allowed by robots.txt" if allowed else "disallowed by robots.txt",
            crawl_delay=float(delay) if delay is not None else None,
        )

    def clear(self) -> None:
        self._cache.clear()
