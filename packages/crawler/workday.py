"""Read Workday listings and bounded details through PoliteFetcher.

A board is a company host: 60 seconds between requests, including POSTs.
A complete listing is required before closing missing jobs. Truncated or
inconsistent pagination closes nothing. At 300 roles the 15 listing pages
span at least 14 minutes; five detail requests add five more minutes.

Descriptions are cached against the listed title, location and URL. Missing
or visibly changed postings take priority, with one rotating validation slot
for description-only edits. Once caught up, all five slots revalidate cached
text. Independent pending and cached cursors live on CompanyCrawlState,
never in the discovery evidence.
A pending posting has no description: existing matching sees its title and
location, no body evidence or body embedding. Hydration emits a normal update
so the ordinary downstream matching path reconsiders it.

The public Ameren list and detail were recorded on 2026-10-20; trimmed public
payloads and provenance are in tests/fixtures/workday. Workday application
forms remain unsupported (CLAUDE.md §11).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import structlog

from packages.crawler.details import DETAIL_REQUESTS_PER_CRAWL, BoardPages, hydrate
from packages.crawler.extract import ExtractedPosting, posting_hash, strip_html
from packages.crawler.fetch import Blocked, PoliteFetcher

log = structlog.get_logger(__name__)

#: Workday's own maximum for this endpoint. Asking for more is not rejected —
#: it is silently truncated to 20, which would make the offset arithmetic walk
#: past postings without ever reporting a gap.
PAGE_SIZE = 20

#: Pages per board per cycle. At §2.6's 60s floor this is an hour of waiting
#: for one company, which is already beyond generous; past it the board is
#: reported truncated rather than quietly halved.
MAX_PAGES = 60

_HOST_RE = re.compile(r"(?P<tenant>[a-z0-9-]+)\.wd[0-9]+\.myworkdayjobs\.com")
_SITE_RE = re.compile(r"[A-Za-z0-9_-]+")


class WorkdayBoardError(RuntimeError):
    """A page failed, so the board was abandoned rather than half-read."""


def parse_site(careers_url: str) -> tuple[str, str, str] | None:
    """`(host, tenant, site)` from a Workday careers URL.

    `https://acme.wd5.myworkdayjobs.com/en-US/AcmeCareers` gives
    `("acme.wd5.myworkdayjobs.com", "acme", "AcmeCareers")`. The locale
    segment is optional and is not part of the API path.

    `None` for anything that is not a Workday careers URL, rather than a
    guess — the slug cannot be derived from a company name here the way it can
    for Greenhouse, because the tenant, the data centre number and the site
    name are three independent facts.
    """
    try:
        parsed = urlparse(careers_url.strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    match = _HOST_RE.fullmatch(host)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not match:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port not in {None, 80, 443}:
        return None
    tenant = match.group("tenant")

    segments = [segment for segment in parsed.path.split("/") if segment]
    # A locale looks like `en-US`; anything else in first position is the site.
    if segments and re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", segments[0], re.I):
        segments = segments[1:]
    if not segments or not _SITE_RE.fullmatch(segments[0]) or segments[0].lower() == "wday":
        return None
    return host, tenant, segments[0]


def api_url(host: str, tenant: str, site: str) -> str:
    return f"https://{host}/wday/cxs/{tenant}/{site}/jobs"


def _external_id(entry: dict[str, Any]) -> str | None:
    """The employer's own requisition id.

    The tail of `externalPath` carries it. A posting
    with no stable id is skipped rather than given a synthetic one — an id
    derived from the title changes when the title is edited, which would read
    as the old posting closing and a new one opening.
    """
    # The public path carries the requisition; bulletFields can instead be
    # locations or time type on a tenant with a different list configuration.
    path = str(entry.get("externalPath") or "")
    match = re.search(r"_([A-Za-z0-9-]+)$", path)
    return match.group(1) if match else None


def parse_page(body: str, host: str, site: str) -> tuple[list[ExtractedPosting], int | None]:
    """Postings on one page, and the board's total if it reported one.

    A malformed body yields `([], None)` rather than raising, matching the
    other extractors — but the caller treats a `None` total on the first page
    as a failure, because the total is what tells the walk when to stop.
    """
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        log.warning("workday_malformed_page", host=host)
        return [], None
    if not isinstance(payload, dict):
        return [], None

    total = payload.get("total")
    total = total if type(total) is int and total >= 0 else None

    entries = payload.get("jobPostings")
    if not isinstance(entries, list):
        return [], None

    postings: list[ExtractedPosting] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        external_id = _external_id(entry)
        path = str(entry.get("externalPath") or "")
        if not external_id or not path.startswith("/job/") or "?" in path or "#" in path:
            continue
        age = re.fullmatch(r"Posted (\d+)\+? Days? Ago", str(entry.get("postedOn") or ""))
        posting = ExtractedPosting(
            external_id=external_id,
            url=f"https://{host}/{site}{path}",
            title=(str(entry.get("title")).strip() if entry.get("title") else None),
            location=(
                str(entry.get("locationsText")).strip() if entry.get("locationsText") else None
            ),
            # Not a mistake, and not laziness: the list endpoint has no
            # description, and `postedOn` is prose — "Posted 30+ Days Ago" —
            # from which no date can be recovered without inventing precision
            # the source does not have. CLAUDE.md keeps an unknown field None.
            description_raw=None,
            published_at=None,
            minimum_age_days=int(age.group(1)) if age else None,
            ats_type="workday",
        )
        posting.content_hash = posting_hash(posting)
        postings.append(posting)

    return postings, total


def read_detail(body: str, listing: ExtractedPosting) -> ExtractedPosting:
    """Read only source facts; a missing/changed schema is not an empty job."""
    try:
        info = json.loads(body)["jobPostingInfo"]
        description = info["jobDescription"]
        if not isinstance(description, str) or not description.strip():
            raise ValueError("missing description")
        published = datetime.fromisoformat(info["startDate"]) if info.get("startDate") else None
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkdayBoardError("malformed Workday detail") from exc
    requisition = info.get("jobReqId")
    if requisition and not re.fullmatch(
        re.escape(str(requisition)) + r"(?:-\d+)?", listing.external_id
    ):
        raise WorkdayBoardError("Workday detail belongs to a different requisition")
    if published is not None and published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    result = listing.model_copy(
        update={
            "description_raw": strip_html(description),
            "published_at": published or listing.published_at,
        }
    )
    result.content_hash = posting_hash(result)
    return result


async def hydrate_details(
    fetcher: PoliteFetcher,
    company_slug: str,
    pages: BoardPages,
    previous: dict[str, ExtractedPosting],
    cursor: str | None,
    *,
    max_age_days: int,
    pending_cursor: str | None = None,
) -> tuple[str | None, str | None]:
    """Prioritize missing/changed text; rotate bounded validation of the rest.

    The budget, the cache and the rotation are `details.hydrate`'s. Workday
    is strict: its detail endpoint is the board's own API, so one failure is
    the board failing.
    """
    host, tenant, site = company_slug.split("|")

    def detail_url(item: ExtractedPosting) -> str:
        path = urlparse(item.url).path.removeprefix(f"/{site}")
        return f"https://{host}/wday/cxs/{tenant}/{site}{path}"

    return await hydrate(
        fetcher,
        pages,
        previous,
        cursor,
        detail_url=detail_url,
        read_detail=read_detail,
        max_age_days=max_age_days,
        pending_cursor=pending_cursor,
        strict=True,
        board_error=WorkdayBoardError,
    )


@dataclass
class WorkdayExtractor:
    """Reads a Workday board across as many pages as it takes."""

    ats: str = "workday"

    def board_url(self, company_slug: str) -> str:
        """The API endpoint, from a `host|tenant|site` slug.

        Workday needs three facts where the others need one, so the registry
        slug carries all three separated by `|`. A careers URL is turned into
        that form by `slug_for`.
        """
        parts = company_slug.split("|")
        if len(parts) != 3:
            raise ValueError(
                f"workday slug must be 'host|tenant|site', got {company_slug!r} — "
                "use workday.slug_for(careers_url) to build one"
            )
        host, tenant, site = parts
        if parse_site(f"https://{host}/{site}") != (host, tenant, site):
            raise ValueError("invalid Workday board identity")
        return api_url(host, tenant, site)

    def parse(self, body: str, company_slug: str) -> list[ExtractedPosting]:
        """One page, for callers that already have a body.

        Present so `WorkdayExtractor` satisfies `PostingExtractor` and can sit
        in the registry beside the others. It reads a single page, so it is
        *not* how a board should be read — `collect` is. A caller using this
        on a 400-role employer gets 20 postings and no indication that 380
        were left behind.
        """
        host, _, site = company_slug.split("|")
        postings, _ = parse_page(body, host, site)
        return postings

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
        return await hydrate_details(
            fetcher,
            company_slug,
            pages,
            previous,
            cursor,
            max_age_days=max_age_days,
            pending_cursor=pending_cursor,
        )

    async def collect(self, fetcher: PoliteFetcher, company_slug: str) -> BoardPages:
        """Walk every page of the board. Raises rather than half-reading it.

        Raises:
            Blocked: robots.txt says no — propagated so the caller reports it
                as a skip rather than a failure, as it does for every board.
            WorkdayBoardError: a page failed. The whole board is abandoned,
                because a partial list is what `_close_missing` would treat as
                evidence that the missing postings had closed.
        """
        host, tenant, site = company_slug.split("|")
        url = api_url(host, tenant, site)
        pages = BoardPages()
        total: int | None = None
        offset = 0

        for page in range(MAX_PAGES):
            response = await fetcher.fetch(
                url,
                method="POST",
                json={
                    "appliedFacets": {},
                    "limit": PAGE_SIZE,
                    "offset": offset,
                    "searchText": "",
                },
            )
            pages.waited_seconds += response.waited
            if not response.ok:
                raise WorkdayBoardError(f"{url}: HTTP {response.status} at offset {offset}")

            postings, page_total = parse_page(response.text, host, site)
            if page == 0:
                if page_total is None:
                    raise WorkdayBoardError(
                        f"{url}: no total in the first page — the payload shape has changed"
                    )
                total = page_total
            pages.bodies.append(response.text)
            pages.postings.extend(postings)

            # A skipped/malformed entry or shifting pagination is not proof
            # that an unseen requisition closed. Refuse closure on that read.
            payload = json.loads(response.text)
            entries = payload.get("jobPostings", [])
            if len(postings) != len(entries) or (page_total is not None and page_total != total):
                pages.truncated = True

            if not postings:
                # An empty page before the total is reached means the board
                # disagrees with its own count. Stopping is right; pretending
                # the walk finished is not, so it is logged.
                if total is not None and len(pages.postings) < total:
                    log.warning(
                        "workday_page_empty_early",
                        host=host,
                        seen=len(pages.postings),
                        total=total,
                    )
                    pages.truncated = True
                break

            offset += PAGE_SIZE
            if total is not None and offset >= total:
                break
        else:
            log.warning(
                "workday_board_truncated",
                host=host,
                pages=MAX_PAGES,
                seen=len(pages.postings),
                total=total,
            )
            pages.truncated = True

        if total is not None and len({p.external_id for p in pages.postings}) != total:
            pages.truncated = True

        log.info(
            "workday_board_read",
            host=host,
            tenant=tenant,
            postings=len(pages.postings),
            total=total,
            pages=len(pages.bodies),
        )
        return pages


def slug_for(careers_url: str) -> str | None:
    """The registry slug for a Workday careers URL, or None if it is not one."""
    parsed = parse_site(careers_url)
    return "|".join(parsed) if parsed else None


__all__ = [
    "MAX_PAGES",
    "PAGE_SIZE",
    "DETAIL_REQUESTS_PER_CRAWL",
    "Blocked",
    "BoardPages",
    "WorkdayBoardError",
    "WorkdayExtractor",
    "api_url",
    "parse_page",
    "parse_site",
    "slug_for",
]
