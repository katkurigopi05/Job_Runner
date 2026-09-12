"""Workday's careers API — the one board that cannot be read in one request.

Two things make it different from the four in `extract.py`, and both are
contract problems rather than parsing problems.

**It is a POST.** The page offset travels in a JSON body, not a query string,
so `PoliteFetcher.fetch` grew a `method`. Nothing about politeness changes: a
POST touches a host exactly as much as a GET, and the same two gates run
before it.

**It is paginated, and the pages are not optional.** Greenhouse hands back
every open posting in one response; Workday hands back twenty and a total. An
extractor that read the first page would report a 400-role employer as having
20 — and worse, `_close_missing` would then close the other 380 on the next
cycle. So `collect` walks every page, and a page that fails abandons the whole
board rather than returning a partial list. A partial board is the input to
closing postings that are open.

## What this costs, which is the part to read before enabling it

A Workday tenant is its own host — `acme.wd5.myworkdayjobs.com` — not a
multi-tenant API. So §2.6's ordinary 60s floor applies, not the 2s shared one,
and it applies *per page*: an employer with 400 open roles is 20 pages, which
is 20 minutes of waiting for one company, once per cycle. `MAX_PAGES` bounds
that at an hour and logs when it truncates, because silently reading half a
board is the failure this module is otherwise built to avoid.

## What it cannot give you

The list endpoint carries titles, locations and links — no descriptions. Those
live behind one detail request per posting, which at 60s a request is eight
hours for a 500-role employer, per cycle. So Workday postings arrive with no
`description_raw`, and the consequence is concrete rather than cosmetic:
`embed_postings` skips a posting with no description, so these never get a
vector and never score on anything but their title.

That is recorded here, and in the `ExtractedPosting` it produces, rather than
worked around. A posting that ranks low because nothing was read is not the
same as one that ranks low because it does not fit, and the matching side has
no way to tell the difference unless this side says so.

## This is not an adapter

CLAUDE.md §11 puts the Workday *adapter* out of scope until after Phase 6,
which is where it stays: nothing here fills a form or submits anything.
Reading a board and applying to it are different capabilities, and applying to
a Workday posting remains unsupported — `packages/ats/` has no Workday
adapter, so an application to one fails as `unsupported_site`, which is the
correct outcome.

## The payload shape here is unverified

This environment has no egress to `*.myworkdayjobs.com`, so the fixture beside
it was written from the API's documented shape rather than recorded from a
live tenant. CLAUDE.md §15 is explicit about what that is worth: the
hand-written Greenhouse fixture had a native `<select>` while the live board
had moved to react-select, the adapter misread every dropdown, and the suite
stayed green throughout. Record a real response with
`python -m scripts.record_workday <careers-url>` before trusting a Workday
row in the registry.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import structlog

from packages.crawler.extract import ExtractedPosting, posting_hash
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


@dataclass
class BoardPages:
    """Every page of a board, and what was parsed out of them."""

    #: Raw bodies in order. Hashed together for board-level change detection,
    #: which is why they are kept rather than discarded after parsing.
    bodies: list[str] = field(default_factory=list)
    postings: list[ExtractedPosting] = field(default_factory=list)
    waited_seconds: float = 0.0
    #: True when `MAX_PAGES` stopped the walk before the board ended. The
    #: caller must not close anything on a truncated read.
    truncated: bool = False

    @property
    def body(self) -> str:
        """One string standing for the whole board, for hashing."""
        return "\n".join(self.bodies)


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
    host = (parsed.hostname or "").lower()
    if not host.endswith(".myworkdayjobs.com"):
        return None
    tenant = host.split(".", 1)[0]
    if not tenant:
        return None

    segments = [segment for segment in parsed.path.split("/") if segment]
    # A locale looks like `en-US`; anything else in first position is the site.
    if segments and re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2})?", segments[0]):
        segments = segments[1:]
    if not segments:
        return None
    return host, tenant, segments[0]


def api_url(host: str, tenant: str, site: str) -> str:
    return f"https://{host}/wday/cxs/{tenant}/{site}/jobs"


def _external_id(entry: dict) -> str | None:
    """The employer's own requisition id.

    `bulletFields` is where Workday puts it on most tenants; the tail of
    `externalPath` carries it otherwise. Neither is guaranteed, and a posting
    with no stable id is skipped rather than given a synthetic one — an id
    derived from the title changes when the title is edited, which would read
    as the old posting closing and a new one opening.
    """
    for value in entry.get("bulletFields") or []:
        text = str(value).strip()
        if text:
            return text
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
    total = int(total) if isinstance(total, int | float) else None

    postings: list[ExtractedPosting] = []
    for entry in payload.get("jobPostings") or []:
        if not isinstance(entry, dict):
            continue
        external_id = _external_id(entry)
        path = str(entry.get("externalPath") or "")
        if not external_id or not path:
            continue
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
            ats_type="workday",
        )
        posting.content_hash = posting_hash(posting)
        postings.append(posting)

    return postings, total


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
    "Blocked",
    "BoardPages",
    "WorkdayBoardError",
    "WorkdayExtractor",
    "api_url",
    "parse_page",
    "parse_site",
    "slug_for",
]
