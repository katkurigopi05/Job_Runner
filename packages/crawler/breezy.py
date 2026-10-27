"""Read Breezy HR boards: a public job list, then each job's own page for its text.

`https://<company>.breezy.hr/json` lists every open posting (title, location,
link, date) without a description. The text is on the posting's own page as a
schema.org JobPosting, read a few pages per crawl by `details.hydrate`.

A board is the company's own subdomain, so §2.6's 60 s floor applies per
company, as it does to any company's host. Nothing here is a shared API.

A portal that does not exist answers 404 ("Career portal not found") at both
its root and `/json`. On 2026-10-26 two of the Breezy boards a search assistant
reported with high confidence for the owner's sheet were exactly that, which
is why a suggested board is probed before it is trusted.

Recorded 2026-10-26 from public boards; trimmed payloads are in
`tests/fixtures/breezy`. Applying through Breezy forms is not supported, so a
Breezy posting is read and matched but applied to by hand.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import structlog

from packages.crawler import jsonld
from packages.crawler.details import BoardPages, hydrate
from packages.crawler.extract import ExtractedPosting, parse_timestamp, posting_hash, strip_html
from packages.crawler.fetch import PoliteFetcher

log = structlog.get_logger(__name__)

#: A DNS label: what a Breezy company subdomain is.
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class BreezyBoardError(RuntimeError):
    """The list could not be read whole, so the board was not read at all."""


def board_host(company_slug: str) -> str:
    slug = company_slug.strip().lower()
    if not SLUG_RE.fullmatch(slug):
        raise ValueError(f"not a Breezy company subdomain: {company_slug!r}")
    return f"{slug}.breezy.hr"


def _place(location: dict[str, Any]) -> str | None:
    def name(value: Any) -> str | None:
        if isinstance(value, dict):
            value = value.get("name")
        return value.strip() if isinstance(value, str) and value.strip() else None

    place = ", ".join(
        part
        for part in (
            name(location.get("city")),
            name(location.get("state")),
            name(location.get("country")),
        )
        if part
    ) or name(location.get("name"))
    if location.get("is_remote"):
        # Read by `matching.locality` like any board's "Remote (...)".
        return f"Remote ({place})" if place else "Remote"
    return place


def _location(entry: dict[str, Any]) -> str | None:
    """Every place a posting lists, in words the location filters read.

    Country names rather than Breezy's two-letter codes (`Lomé, TG`): a filter
    for Togo matches the word, not the code.
    """
    locations = entry.get("locations") or [entry.get("location")]
    places: list[str] = []
    for location in locations:
        if isinstance(location, dict):
            place = _place(location)
            if place and place not in places:
                places.append(place)
    return "; ".join(places) or None


def parse_list(body: str, company_slug: str) -> tuple[list[ExtractedPosting], int]:
    """The board's postings, and how many entries the list held.

    Raises `BreezyBoardError` when the body is not Breezy's list. An entry
    without an id is skipped, and the count lets the caller see that it was.
    """
    host = board_host(company_slug)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise BreezyBoardError("Breezy list is not JSON") from exc
    if not isinstance(payload, list):
        raise BreezyBoardError("Breezy list is not a list")

    postings: list[ExtractedPosting] = []
    for entry in payload:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        url = str(entry.get("url") or "")
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != host or not parsed.path.startswith("/p/"):
            friendly = str(entry.get("friendly_id") or entry["id"])
            url = f"https://{host}/p/{friendly}"
        posting = ExtractedPosting(
            external_id=str(entry["id"]),
            url=url,
            title=(entry.get("name") or "").strip() or None,
            location=_location(entry),
            ats_type="breezy",
            published_at=parse_timestamp(entry.get("published_date")),
        )
        posting.content_hash = posting_hash(posting)
        postings.append(posting)
    return postings, len(payload)


def read_detail(body: str, listing: ExtractedPosting) -> ExtractedPosting:
    """The posting's text from its page's schema.org JobPosting."""
    for node in jsonld.job_postings(body):
        description = strip_html(
            node.get("description") if isinstance(node.get("description"), str) else None
        )
        if description:
            result = listing.model_copy(
                update={
                    "description_raw": description,
                    "published_at": listing.published_at or parse_timestamp(node.get("datePosted")),
                }
            )
            result.content_hash = posting_hash(result)
            return result
    raise BreezyBoardError("Breezy job page has no JobPosting description")


@dataclass
class BreezyExtractor:
    """One list request per board; descriptions from job pages, a few per crawl."""

    ats: str = "breezy"

    def board_url(self, company_slug: str) -> str:
        return f"https://{board_host(company_slug)}/json"

    def parse(self, body: str, company_slug: str) -> list[ExtractedPosting]:
        postings, _ = parse_list(body, company_slug)
        return postings

    async def collect(self, fetcher: PoliteFetcher, company_slug: str) -> BoardPages:
        """Read the whole list, or raise: a partial list would close live postings."""
        url = self.board_url(company_slug)
        response = await fetcher.fetch(url)
        pages = BoardPages(waited_seconds=response.waited)
        if not response.ok:
            raise BreezyBoardError(f"{url}: HTTP {response.status}")
        postings, entries = parse_list(response.text, company_slug)
        pages.bodies.append(response.text)
        pages.postings.extend(postings)
        # A skipped entry is not proof that an unseen posting closed.
        pages.truncated = len(postings) != entries
        log.info("breezy_board_read", company=company_slug, postings=len(postings))
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
        host = board_host(company_slug)

        def detail_url(item: ExtractedPosting) -> str:
            if urlparse(item.url).hostname != host:
                raise BreezyBoardError("posting is not on this board")
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
            board_error=BreezyBoardError,
        )


def slug_for(url: str) -> str | None:
    """The company subdomain for any URL on a Breezy board, or None."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host.endswith(".breezy.hr"):
        return None
    slug = host.removesuffix(".breezy.hr")
    return slug if SLUG_RE.fullmatch(slug) and slug not in {"www", "app", "api"} else None
