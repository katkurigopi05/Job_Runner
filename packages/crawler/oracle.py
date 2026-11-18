"""Oracle Candidate Experience's public list/detail APIs, through PoliteFetcher.

Only explicit oraclecloud.com career-site URLs are recognized. No account,
internal requisition, application or secured endpoint is read. Fixture provenance
records the site's own JavaScript requests. Lists are bounded; incomplete or
inconsistent lists never authorize closing held jobs. Five rotating details use
the existing hydration budget/cache; the list alone has no full description.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import partial
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from packages.crawler.details import BoardPages, hydrate
from packages.crawler.extract import ExtractedPosting, parse_timestamp, posting_hash, strip_html
from packages.crawler.fetch import Blocked, PoliteFetcher

PAGE_SIZE = 25
MAX_PAGES = 100
_HOST = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.oraclecloud\.com")
_SITE = re.compile(r"[A-Za-z0-9_-]+")
_PATH = re.compile(r"/hcmUI/CandidateExperience/[a-zA-Z-]+/sites/([A-Za-z0-9_-]+)(?:/|$)")


class OracleBoardError(RuntimeError):
    """No complete public board was read; keep held postings open."""


def slug_for(url: str) -> str | None:
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        match = _PATH.match(p.path)
        if p.scheme != "https" or p.username or p.password or p.port not in (None, 443):
            return None
        if _HOST.fullmatch(host) and match:
            return f"{host}|{match[1]}"
    except ValueError:
        pass
    return None


def _site(slug: str) -> tuple[str, str]:
    parts = slug.split("|")
    if len(parts) != 2 or not _HOST.fullmatch(parts[0]) or not _SITE.fullmatch(parts[1]):
        raise ValueError("not an Oracle public career-site identity")
    return parts[0], parts[1]


def list_url(slug: str, offset: int = 0) -> str:
    host, site = _site(slug)
    query = urlencode(
        {
            "onlyData": "true",
            "expand": "requisitionList.secondaryLocations",
            "finder": (
                f"findReqs;siteNumber={site},facetsList=NONE,limit={PAGE_SIZE},offset={offset}"
            ),
        }
    )
    return f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?{query}"


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _locations(entry: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    primary = _text(entry.get("PrimaryLocation"))
    country = _text(entry.get("PrimaryLocationCountry"))
    mode = _text(entry.get("WorkplaceType"))
    remote = {"remote": True, "on-site": False, "onsite": False, "hybrid": False}.get(
        (mode or "").lower()
    )
    offices: list[dict[str, Any]] = []
    if primary:
        offices.append({"raw": primary, "country_code": country, "source_remote": remote})
    elif country:
        offices.append({"raw": country, "country_code": country, "source_remote": remote})
    secondary = entry.get("secondaryLocations")
    if not isinstance(secondary, list):
        raise OracleBoardError("secondary locations are missing or malformed")
    for office in secondary:
        if not isinstance(office, dict) or not (raw := _text(office.get("Name"))):
            raise OracleBoardError("secondary office has no source name")
        offices.append({"raw": raw, "source_remote": remote})
    return "; ".join(dict.fromkeys(o["raw"] for o in offices)) or None, offices


def parse_page(body: str, slug: str) -> tuple[list[ExtractedPosting], int, int]:
    host, site = _site(slug)
    try:
        payload = json.loads(body)
        items = payload["items"]
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise ValueError("one search envelope required")
        result = items[0]
        total, offset = result["TotalJobsCount"], result["Offset"]
        if type(total) is not int or total < 0 or type(offset) is not int or offset < 0:
            raise ValueError("invalid count or offset")
        if result.get("SiteNumber") != site:
            raise ValueError("search envelope belongs to another site")
        entries = result["requisitionList"]
        if not isinstance(entries, list):
            raise ValueError("not a requisition list")
    except (ValueError, KeyError, TypeError) as exc:
        raise OracleBoardError("not a public Oracle search response") from exc
    postings = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise OracleBoardError("invalid requisition")
        job_id = _text(entry.get("Id"))
        title = _text(entry.get("Title"))
        if not job_id or not _SITE.fullmatch(job_id) or not title:
            raise OracleBoardError("requisition lacks a stable id/title")
        location, offices = _locations(entry)
        item = ExtractedPosting(
            external_id=job_id,
            title=title,
            location=location,
            listed_location=location,
            source_locations=offices,
            ats_type="oracle",
            url=(
                f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}"
                f"/job/{quote(job_id, safe='')}"
            ),
            published_at=parse_timestamp(entry.get("PostedDate")),
        )
        item.content_hash = posting_hash(item)
        postings.append(item)
    return postings, total, offset


def read_detail(body: str, listing: ExtractedPosting) -> ExtractedPosting:
    try:
        payload = json.loads(body)
        entries = payload["items"]
        if len(entries) != 1 or str(entries[0]["Id"]) != listing.external_id:
            raise ValueError("detail belongs to another listing")
        entry = entries[0]
        description = "\n\n".join(
            dict.fromkeys(
                text
                for key in (
                    "ExternalDescriptionStr",
                    "ExternalResponsibilitiesStr",
                    "ExternalQualificationsStr",
                )
                if (text := strip_html(_text(entry.get(key))))
            )
        )
        if not description:
            raise ValueError("no public description")
        location, offices = _locations(entry)
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise OracleBoardError("not this posting's public detail") from exc
    result = listing.model_copy(
        update={
            "description_raw": description,
            "location": location,
            "source_locations": offices,
            "published_at": parse_timestamp(entry.get("ExternalPostedStartDate"))
            or listing.published_at,
        }
    )
    result.content_hash = posting_hash(result)
    return result


def detail_url_for(slug: str, item: ExtractedPosting) -> str:
    host, site = _site(slug)
    if slug_for(item.url) != slug:
        raise OracleBoardError("posting belongs to another board")
    query = urlencode(
        {
            "expand": "all",
            "onlyData": "true",
            "finder": f'ById;Id="{item.external_id}",siteNumber={site}',
        }
    )
    return f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?{query}"


@dataclass
class OracleExtractor:
    ats: str = "oracle"

    def board_url(self, company_slug: str) -> str:
        return list_url(company_slug)

    def parse(self, body: str, company_slug: str) -> list[ExtractedPosting]:
        return parse_page(body, company_slug)[0]

    async def collect(self, fetcher: PoliteFetcher, company_slug: str) -> BoardPages:
        pages = BoardPages()
        seen: set[str] = set()
        expected: int | None = None
        offset = 0
        for _ in range(MAX_PAGES):
            response = await fetcher.fetch(list_url(company_slug, offset))
            pages.waited_seconds += response.waited
            if not response.ok:
                raise OracleBoardError(f"Oracle list HTTP {response.status}")
            jobs, total, reported_offset = parse_page(response.text, company_slug)
            pages.bodies.append(response.text)
            if expected is None:
                expected = total
            if total != expected or reported_offset != offset:
                pages.truncated = True
                break
            for item in jobs:
                if item.external_id in seen:
                    pages.truncated = True
                else:
                    seen.add(item.external_id)
                    pages.postings.append(item)
            offset += len(jobs)
            if pages.truncated or offset >= total or len(jobs) < PAGE_SIZE:
                pages.truncated = pages.truncated or len(seen) != total
                return pages
        pages.truncated = True
        return pages

    async def hydrate(
        self,
        fetcher: PoliteFetcher,
        company_slug: str,
        pages: BoardPages,
        previous: dict[str, ExtractedPosting],
        cursor: str | None,
        pending_cursor: str | None,
        *,
        max_age_days: int,
    ) -> tuple[str | None, str | None]:
        return await hydrate(
            fetcher,
            pages,
            previous,
            cursor,
            detail_url=partial(detail_url_for, company_slug),
            read_detail=read_detail,
            max_age_days=max_age_days,
            pending_cursor=pending_cursor,
            strict=True,
            board_error=OracleBoardError,
        )


async def probe(fetcher: PoliteFetcher, slug: str) -> tuple[int | None, str, str | None]:
    url = list_url(slug)
    try:
        result = await fetcher.fetch(url)
        if result.ok:
            jobs, total, offset = parse_page(result.text, slug)
            if offset == 0 and (jobs or total == 0):
                return total, url, None
    except Blocked as exc:
        return None, url, str(exc)
    except (OracleBoardError, ValueError):
        pass
    return None, url, None
