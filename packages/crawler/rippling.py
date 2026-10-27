"""Read Rippling boards: paged lists from the board page, each job's text from its own.

`https://ats.rippling.com/<slug>/jobs?page=N` is a page whose embedded data
(`__NEXT_DATA__`) carries that page of postings, 20 at a time, with the board's
total. The list has no description and no date. Each job's page carries both
under `apiData.jobPost`: the text in two HTML parts, `role` and `company`, and
`createdOn`. Those are read a few pages per crawl by `details.hydrate`.

Every Rippling board is served from the one host, `ats.rippling.com`. It is not
in `ratelimit.SHARED_API_HOSTS`. That list is the owner's narrowing of §2.6 and
is changed only by them, so until it is, every Rippling company waits behind
one 60 s counter.

Recorded 2026-10-26 from a public board; trimmed payloads are in
`tests/fixtures/rippling`. Applying through Rippling forms is not supported, so
a Rippling posting is read and matched but applied to by hand.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import structlog

from packages.crawler.details import BoardPages, hydrate
from packages.crawler.extract import ExtractedPosting, parse_timestamp, posting_hash, strip_html
from packages.crawler.fetch import PoliteFetcher

log = structlog.get_logger(__name__)

HOST = "ats.rippling.com"
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
#: 20 postings a page, so 500 roles. Past it the read is reported truncated
#: and nothing is closed, rather than quietly read in part.
MAX_PAGES = 25
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class RipplingBoardError(RuntimeError):
    """A page could not be read, so the board was not read at all."""


def _slug(company_slug: str) -> str:
    slug = company_slug.strip()
    if not SLUG_RE.fullmatch(slug):
        raise ValueError(f"not a Rippling board slug: {company_slug!r}")
    return slug


def _next_data(body: str) -> dict[str, Any]:
    match = _NEXT_DATA_RE.search(body)
    if not match:
        raise RipplingBoardError("Rippling page has no embedded data")
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RipplingBoardError("Rippling embedded data is not JSON") from exc
    if not isinstance(data, dict):
        raise RipplingBoardError("Rippling embedded data is not an object")
    return data


def _job_posts(data: dict[str, Any]) -> dict[str, Any]:
    """The `job-posts` query's result: items plus page, totalPages, totalItems."""
    try:
        queries = data["props"]["pageProps"]["dehydratedState"]["queries"]
    except (KeyError, TypeError) as exc:
        raise RipplingBoardError("Rippling page has no job list") from exc
    for query in queries if isinstance(queries, list) else []:
        key = query.get("queryKey") if isinstance(query, dict) else None
        if isinstance(key, list) and len(key) > 2 and key[0] == "board" and key[2] == "job-posts":
            result = (query.get("state") or {}).get("data")
            if isinstance(result, dict) and isinstance(result.get("items"), list):
                return result
    raise RipplingBoardError("Rippling page has no job list")


def _location(item: dict[str, Any]) -> str | None:
    places: list[str] = []
    for location in item.get("locations") or []:
        if not isinstance(location, dict):
            continue
        name = location.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        if location.get("workplaceType") == "REMOTE" and "remote" not in name.lower():
            name = f"Remote ({name})"
        if name not in places:
            places.append(name)
    return "; ".join(places) or None


def merge_rows(rows: list[ExtractedPosting]) -> list[ExtractedPosting]:
    """One posting per job, carrying every place it is listed in.

    Rippling lists a job once per location, under the same id: on 2026-10-26
    `branch` reported 24 items that were 14 jobs. Stored as rows they would be
    one posting overwritten by each of its locations in turn.
    """
    merged: dict[str, ExtractedPosting] = {}
    for row in rows:
        held = merged.get(row.external_id)
        if held is None:
            merged[row.external_id] = row.model_copy()
            continue
        places = (held.location or "").split("; ") if held.location else []
        for place in (row.location or "").split("; ") if row.location else []:
            if place not in places:
                places.append(place)
        held.location = "; ".join(places) or None
    for posting in merged.values():
        posting.content_hash = posting_hash(posting)
    return list(merged.values())


def parse_page(
    body: str, company_slug: str
) -> tuple[list[ExtractedPosting], int | None, int | None, int]:
    """One page's rows (one per job and location), the board's totals, its entry count.

    `totalItems` counts rows, not jobs, so completeness is judged by rows read.
    """
    slug = _slug(company_slug)
    result = _job_posts(_next_data(body))
    postings: list[ExtractedPosting] = []
    for item in result["items"]:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        job_id = str(item["id"])
        url = f"https://{HOST}/{slug}/jobs/{job_id}"
        posting = ExtractedPosting(
            external_id=job_id,
            url=url,
            title=(item.get("name") or "").strip() or None,
            location=_location(item),
            ats_type="rippling",
        )
        posting.content_hash = posting_hash(posting)
        postings.append(posting)
    pages = result.get("totalPages")
    items = result.get("totalItems")
    return (
        postings,
        pages if isinstance(pages, int) else None,
        items if isinstance(items, int) else None,
        len(result["items"]),
    )


def read_detail(body: str, listing: ExtractedPosting) -> ExtractedPosting:
    """The role text, then the company's, and the date the job was created."""
    try:
        post = _next_data(body)["props"]["pageProps"]["apiData"]["jobPost"]
        parts = post["description"]
    except (KeyError, TypeError) as exc:
        raise RipplingBoardError("Rippling job page has no description") from exc
    if str(post.get("uuid") or "") != listing.external_id:
        raise RipplingBoardError("Rippling job page belongs to a different posting")
    text = "\n\n".join(
        part
        for part in (
            strip_html(parts.get("role") if isinstance(parts, dict) else None),
            strip_html(parts.get("company") if isinstance(parts, dict) else None),
        )
        if part
    )
    if not text:
        raise RipplingBoardError("Rippling job page has an empty description")
    result = listing.model_copy(
        update={
            "description_raw": text,
            "published_at": parse_timestamp(post.get("createdOn")) or listing.published_at,
        }
    )
    result.content_hash = posting_hash(result)
    return result


@dataclass
class RipplingExtractor:
    """Every page of a board's list; descriptions from job pages, a few per crawl."""

    ats: str = "rippling"

    def board_url(self, company_slug: str) -> str:
        return f"https://{HOST}/{_slug(company_slug)}/jobs"

    def parse(self, body: str, company_slug: str) -> list[ExtractedPosting]:
        """One page, for callers that already have a body. `collect` reads them all."""
        rows, _, _, _ = parse_page(body, company_slug)
        return merge_rows(rows)

    async def collect(self, fetcher: PoliteFetcher, company_slug: str) -> BoardPages:
        """Walk every page. Raises rather than half-reading the board."""
        url = self.board_url(company_slug)
        pages = BoardPages()
        total_pages: int | None = None
        total_items: int | None = None
        rows: list[ExtractedPosting] = []
        for page in range(MAX_PAGES):
            response = await fetcher.fetch(url if page == 0 else f"{url}?page={page}")
            pages.waited_seconds += response.waited
            if not response.ok:
                raise RipplingBoardError(f"{url}: HTTP {response.status} on page {page}")
            page_rows, page_count, item_count, entries = parse_page(response.text, company_slug)
            if page == 0:
                total_pages, total_items = page_count, item_count
            elif (page_count, item_count) != (total_pages, total_items):
                # The board changed while it was being read.
                pages.truncated = True
            pages.bodies.append(response.text)
            rows.extend(page_rows)
            if len(page_rows) != entries:
                pages.truncated = True
            if total_pages is None or page + 1 >= total_pages or not page_rows:
                break
        else:
            log.warning("rippling_board_truncated", company=company_slug, pages=MAX_PAGES)
            pages.truncated = True
        # Rows read against rows reported: a job listed in three cities is three.
        if total_items is None or len(rows) != total_items:
            pages.truncated = True
        pages.postings = merge_rows(rows)
        log.info(
            "rippling_board_read",
            company=company_slug,
            postings=len(pages.postings),
            total=total_items,
            pages=len(pages.bodies),
        )
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
        prefix = f"/{_slug(company_slug)}/jobs/"

        def detail_url(item: ExtractedPosting) -> str:
            parsed = urlparse(item.url)
            if parsed.hostname != HOST or not parsed.path.startswith(prefix):
                raise RipplingBoardError("posting is not on this board")
            return item.url

        return await hydrate(
            fetcher,
            pages,
            previous,
            cursor,
            detail_url=detail_url,
            read_detail=read_detail,
            max_age_days=max_age_days,
            pending_cursor=pending_cursor,
            strict=False,
            board_error=RipplingBoardError,
        )


def slug_for(url: str) -> str | None:
    """The board slug for a Rippling board or job URL, or None."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() != HOST:
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments or (len(segments) > 1 and segments[1] != "jobs"):
        return None
    slug = segments[0]
    return slug if SLUG_RE.fullmatch(slug) and slug.lower() not in {"api", "internal"} else None


def is_board_page(body: str) -> bool:
    """Whether a page is a Rippling board, empty or not (`validate`'s question)."""
    try:
        _job_posts(_next_data(body))
    except RipplingBoardError:
        return False
    return True
