"""Read the public iCIMS/Jibe careers search API, never the authenticated ATS.

The careers page itself calls /api/jobs. Custom hosts are recognized only from
its Jibe page configuration AND vendor script. Legacy *.icims.com portals are
not claimed by this reader. Public fixture provenance is in tests/fixtures/icims.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from packages.crawler.details import BoardPages
from packages.crawler.extract import ExtractedPosting, parse_timestamp, posting_hash, strip_html
from packages.crawler.fetch import Blocked, PoliteFetcher

PAGE_SIZE = 10
MAX_PAGES = 200


class IcimsBoardError(RuntimeError):
    """A public board was refused or incomplete; never close missing postings."""


def board_origin(slug: str) -> str:
    try:
        p = urlparse(slug)
        if (
            p.scheme == "https"
            and p.hostname
            and "." in p.hostname
            and not p.username
            and not p.password
            and p.port in (None, 443)
            and p.path in ("", "/")
            and not p.query
            and not p.fragment
        ):
            return f"https://{p.hostname.lower()}"
    except ValueError:
        pass
    raise ValueError("iCIMS/Jibe board identity must be an HTTPS origin")


class _PageScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.vendor = False
        self.inline = False
        self.config = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        src = dict(attrs).get("src")
        self.inline = not src
        if src:
            try:
                p = urlparse(src)
                self.vendor = self.vendor or (
                    p.scheme == "https"
                    and p.hostname == "app.jibecdn.com"
                    and (p.path.endswith("/cdn.js") or "/search/" in p.path)
                )
            except ValueError:
                pass

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.inline = False

    def handle_data(self, data: str) -> None:
        if self.inline and re.search(r'window\._jibe\s*=\s*\{\s*["\x27]cid["\x27]\s*:', data):
            self.config = True


def from_page(body: str, page_url: str) -> str | None:
    parser = _PageScripts()
    parser.feed(body)
    if not (parser.vendor and parser.config):
        return None
    try:
        p = urlparse(page_url)
        # The final fetched origin, not a guessed employer slug.
        return board_origin(f"{p.scheme}://{p.netloc}")
    except ValueError:
        return None


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _locations(entry: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    extra = entry.get("additional_locations", [])
    if not isinstance(extra, list) or any(not isinstance(o, dict) for o in extra):
        raise IcimsBoardError("invalid additional locations")
    offices: list[dict[str, Any]] = []
    for office in [entry, *extra]:
        raw = ", ".join(
            part for key in ("city", "state", "country") if (part := _text(office.get(key)))
        ) or _text(office.get("location_name"))
        country = _text(office.get("country_code")) or _text(office.get("country"))
        raw = raw or country
        if raw:
            offices.append({"raw": raw, "country_code": country})
    if entry.get("multipleLocations") is True and not extra:
        # Do not classify the unreported other offices from the primary one.
        offices.append({"raw": "Additional locations", "country_code": None})
    return "; ".join(dict.fromkeys(o["raw"] for o in offices)) or None, offices


def parse_page(body: str, slug: str) -> tuple[list[ExtractedPosting], int]:
    origin = board_origin(slug)
    try:
        payload = json.loads(body)
        entries, total = payload["jobs"], payload["totalCount"]
        if not isinstance(entries, list) or type(total) is not int or total < 0:
            raise ValueError("invalid list or total")
    except (ValueError, TypeError, KeyError) as exc:
        raise IcimsBoardError("not a public iCIMS/Jibe search response") from exc
    postings = []
    for row in entries:
        if not isinstance(row, dict) or not isinstance(row.get("data"), dict):
            raise IcimsBoardError("invalid job entry")
        entry = row["data"]
        job_id = _text(entry.get("req_id"))
        path_id = _text(entry.get("slug"))
        title = _text(entry.get("title"))
        if not job_id or not path_id or not title or entry.get("internal") is True:
            raise IcimsBoardError("job lacks public stable identity/title")
        location, offices = _locations(entry)
        item = ExtractedPosting(
            external_id=job_id,
            title=title,
            location=location,
            source_locations=offices,
            ats_type="icims",
            url=f"{origin}/jobs/{quote(path_id, safe='')}",
            description_raw=strip_html(_text(entry.get("description"))),
            published_at=parse_timestamp(entry.get("posted_date")),
        )
        item.content_hash = posting_hash(item)
        postings.append(item)
    return postings, total


def list_url(slug: str, page: int = 1) -> str:
    query = urlencode({"page": page, "limit": PAGE_SIZE})
    return f"{board_origin(slug)}/api/jobs?{query}"


@dataclass
class IcimsExtractor:
    ats: str = "icims"

    def board_url(self, company_slug: str) -> str:
        return list_url(company_slug)

    def parse(self, body: str, company_slug: str) -> list[ExtractedPosting]:
        return parse_page(body, company_slug)[0]

    async def collect(self, fetcher: PoliteFetcher, company_slug: str) -> BoardPages:
        pages = BoardPages()
        seen: set[str] = set()
        expected: int | None = None
        for page in range(1, MAX_PAGES + 1):
            response = await fetcher.fetch(list_url(company_slug, page))
            pages.waited_seconds += response.waited
            if not response.ok:
                raise IcimsBoardError(f"iCIMS list HTTP {response.status}")
            jobs, total = parse_page(response.text, company_slug)
            pages.bodies.append(response.text)
            if expected is None:
                expected = total
            if total != expected:
                pages.truncated = True
                break
            for item in jobs:
                if item.external_id in seen:
                    pages.truncated = True
                else:
                    seen.add(item.external_id)
                    pages.postings.append(item)
            if pages.truncated or len(seen) >= total or len(jobs) < PAGE_SIZE:
                pages.truncated = pages.truncated or len(seen) != total
                return pages
        pages.truncated = True
        return pages


async def probe(fetcher: PoliteFetcher, slug: str) -> tuple[int | None, str, str | None]:
    url = list_url(slug)
    try:
        result = await fetcher.fetch(url)
        if result.ok:
            jobs, total = parse_page(result.text, slug)
            if jobs or total == 0:
                return total, url, None
    except Blocked as exc:
        return None, url, str(exc)
    except (IcimsBoardError, ValueError):
        pass
    return None, url, None
