"""robots.txt — CLAUDE.md §2.6.

Two decisions worth stating, because both cut against convenience:

- **An unreachable robots.txt means do not crawl; an unavailable one does
  not.** RFC 9309 draws the line at the status class, and so does this. A 5xx
  or a network failure is "unreachable" (§2.3.1.4) — the file is undefined and
  the crawler "MUST assume complete disallow", because a site having a bad day
  is not a site granting permission. Any 4xx is "unavailable" (§2.3.1.3): the
  server is telling us there is no rules file, and the crawler "MAY access any
  resources on the server".

  This distinction was 404-only once, which is stricter than the standard and
  cost real coverage: `api.ashbyhq.com` answers 401 for `/robots.txt`, so
  every Ashby board was refused here and that extractor crawled nothing.
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

        if 400 <= response.status_code < 500:
            # RFC 9309 §2.3.1.3 "Unavailable": any 4xx means the file is
            # unavailable, and "the crawler MAY access any resources on the
            # server". 404 is the common case; 401 and 403 say the same thing
            # about the *rules file*, not about the content.
            #
            # This was 404-only, and the difference was not theoretical:
            # api.ashbyhq.com answers 401 for /robots.txt, so every Ashby board
            # was refused at this gate and one of four extractors crawled
            # nothing at all. Being stricter than the standard is not more
            # respectful when the site published no rules to respect.
            if response.status_code != 404:
                log.info("robots_unavailable", host=host, status=response.status_code)
            entry = _CachedRobots(
                parser=None, fetched_at=time.monotonic(), reachable=True, missing=True
            )
            self._cache[host] = entry
            return entry

        if response.status_code >= 500:
            # RFC 9309 §2.3.1.4 "Unreachable": a server error means the file is
            # undefined and the crawler "MUST assume complete disallow". This
            # is the case the permissive reading gets wrong — a site having a
            # bad day is not a site granting permission.
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
