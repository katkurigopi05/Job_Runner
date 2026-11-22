"""Bounded search and page reads for public interview research."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import socket
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

import httpx

from packages.core.config import get_settings
from packages.crawler.fetch import PoliteFetcher
from packages.crawler.ratelimit import HostRateLimiter
from packages.interview.plan import evidence
from packages.interview.schema import Evidence, Snapshot, SourceKind

_LIMITER = HostRateLimiter()
SENIOR = re.compile(r"\b(?:staff|principal|senior|sr\.?|lead)\b", re.I)
JUNIOR = re.compile(r"\b(?:new.grad(?:uate)?|junior|entry.level|intern|early.career)\b", re.I)
FIRSTHAND = re.compile(r"\bI (?:interviewed|applied|was asked|had an interview)\b", re.I)


def public_url(raw: str) -> str:
    parts = urlsplit(raw)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.port not in {None, 80 if parts.scheme == "http" else 443}
    ):
        raise ValueError("Expected a public HTTP or HTTPS address.")
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, ""))


class PublicTransport(httpx.AsyncBaseTransport):
    """Pin each request, including redirects and robots, to a public DNS answer."""

    def __init__(self) -> None:
        self.last_url = ""
        self.inner = httpx.AsyncHTTPTransport(
            trust_env=False, limits=httpx.Limits(max_keepalive_connections=0)
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = httpx.URL(public_url(str(request.url)))
        port = url.port or (443 if url.scheme == "https" else 80)
        answers = await asyncio.get_running_loop().getaddrinfo(
            url.host, port, type=socket.SOCK_STREAM
        )
        addresses = [ipaddress.ip_address(answer[4][0]) for answer in answers]
        if not addresses or any(not addr.is_global or addr.is_multicast for addr in addresses):
            raise ValueError("Only publicly routable page addresses are supported.")
        pinned = httpx.Request(
            request.method,
            url.copy_with(host=str(addresses[0])),
            headers={**request.headers, "host": url.netloc.decode()},
            extensions={**request.extensions, "sni_hostname": url.host},
        )
        response = await self.inner.handle_async_request(pinned)
        self.last_url = str(url)
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


class Page(HTMLParser):
    hidden = 0
    published: date | None = None

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"style", "script", "noscript", "template"}:
            self.hidden += 1
        if tag == "meta":
            meta = dict(attrs)
            if (meta.get("property") or meta.get("name")) in {
                "article:published_time",
                "datePublished",
                "date",
            }:
                with contextlib.suppress(ValueError):
                    self.published = date.fromisoformat((meta.get("content") or "")[:10])

    def handle_endtag(self, tag: str) -> None:
        if self.hidden and tag in {"style", "script", "noscript", "template"}:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def scope(text: str, expected: Snapshot) -> tuple[str | None, str]:
    senior, junior = SENIOR.search(text), JUNIOR.search(text)
    level = senior[0] if senior else junior[0] if junior else None
    if senior and JUNIOR.search(expected.role):
        return level, "Weak match. This source describes a senior role."
    if junior and SENIOR.search(expected.role):
        return level, "Weak match. This source describes an early-career role."
    return level, ""


def public_query(snapshot: Snapshot, intent: str) -> str:
    fields = [snapshot.company, snapshot.role, snapshot.location]
    clean = []
    for field in fields:
        field = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|https?://\S+", "", field)
        field = re.sub(r"(?:\+?[0-9][ .()-]*){7,}", "", field)
        clean.append(re.sub(r"[^\w &./+-]", " ", field)[:90])
    suffix = (
        "official interview hiring process"
        if intent == "official"
        else "candidate interview experience"
    )
    # Only use a round phrase when it appears in the public posting itself.
    public_round = re.search(
        r"\b(technical screen|coding interview|phone screen|onsite interview)\b",
        snapshot.posting_description,
        re.I,
    )
    if intent == "format":
        suffix = "interview format coding assessment"
    parts = [*clean, public_round[0] if public_round else "", suffix]
    return " ".join(part.strip() for part in parts if part.strip())


async def search(snapshot: Snapshot, intent: str) -> list[str]:
    token = get_settings().interview_brave_api_key
    if not token or not snapshot.company:
        return []
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
        response = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": public_query(snapshot, intent), "count": 4},
            headers={"X-Subscription-Token": token.get_secret_value()},
        )
        response.raise_for_status()
        rows = response.json().get("web", {}).get("results", [])
    result = []
    for row in rows[:4]:
        try:
            result.append(public_url(row["url"]))
        except (KeyError, TypeError, ValueError):
            continue
    return list(dict.fromkeys(result))


async def read(raw: str, snapshot: Snapshot) -> Evidence | None:
    url = public_url(raw)
    transport = PublicTransport()
    async with PoliteFetcher(
        rate_limiter=_LIMITER,
        transport=transport,
        timeout=15,
        record_metrics=False,
    ) as fetcher:
        result = await fetcher.fetch(url)
    if not result.ok:
        return None
    page = Page()
    page.feed(result.text)
    final_url = transport.last_url or url
    origin = urlsplit(final_url).hostname or ""
    domain = (urlsplit("//" + snapshot.company_domain).hostname or "").lower()
    selected = [
        part
        for part in page.parts
        if re.search(r"\binterview|\bassessment|\btechnical screen", part, re.I)
    ]
    excerpt = " ".join(selected or page.parts)[:2400].strip()
    if not excerpt:
        return None
    kind: SourceKind
    if domain and (origin == domain or origin.endswith("." + domain)) and selected:
        kind = "official employer guidance"
    elif FIRSTHAND.search(excerpt):
        kind = "firsthand report"
    else:
        kind = "unverified public source"
    level, caveat = scope(excerpt, snapshot)
    role = (
        snapshot.role if snapshot.role and snapshot.role.casefold() in excerpt.casefold() else None
    )
    location = (
        snapshot.location
        if snapshot.location and snapshot.location.casefold() in excerpt.casefold()
        else None
    )
    round_name = (
        snapshot.details.round
        if snapshot.details.round and snapshot.details.round.casefold() in excerpt.casefold()
        else None
    )
    if not round_name:
        known_round = re.search(
            r"\b(?:round \d+|technical screen|phone screen|coding interview|"
            r"system design (?:interview|round)|final round)\b",
            excerpt,
            re.I,
        )
        round_name = known_round[0] if known_round else None
    if kind == "firsthand report" and not role:
        found_role = re.search(
            r"\bfor (?:a |an |the )?((?:senior |staff |junior |new.grad )?"
            r"(?:software engineer|applied ai engineer|ml engineer|agentic ai engineer))\b",
            excerpt,
            re.I,
        )
        role = found_role[1] if found_role else None
    mismatched_round = bool(
        round_name
        and snapshot.details.round
        and snapshot.details.round.casefold() != round_name.casefold()
    )
    if (
        kind == "firsthand report"
        and not caveat
        and (not role or not round_name or mismatched_round)
    ):
        caveat = "Weak match. The same role and round could not be verified."
    return evidence(
        kind,
        final_url,
        excerpt,
        url=final_url,
        published=page.published,
        retrieved=datetime.now(UTC).isoformat(),
        level=level,
        role=role,
        location=location,
        round=round_name,
        caveat=caveat,
        summary=" ".join(excerpt.split()[:35]),
    )
