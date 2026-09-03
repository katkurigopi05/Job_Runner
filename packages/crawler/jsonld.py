"""Read schema.org `JobPosting` out of an ordinary careers page.

The registry only holds companies whose careers URL is a board we have an
adapter for. `scripts/import_portals.py` says why the rest are skipped:

    A company whose careers page is its own site (`twilio.com/careers`) is
    reported and skipped: we have no extractor for a bespoke page, so adding it
    to the registry would mean a crawl cycle that fetches and parses nothing
    every hour, forever.

This is that extractor, and `seeds/bespoke_careers.csv` — written by
`make import-csv` — is its input.

## Why JSON-LD rather than CSS selectors

Career sites publish `JobPosting` structured data **so that machines read it**:
it is what Google Jobs indexes, and a site that wants its roles found keeps it
accurate. That makes it the one part of a bespoke page with a stable contract.

The alternative is guessing selectors per site and re-guessing when the design
changes. CLAUDE.md records what that costs: a Greenhouse fixture had native
`<select>` while the live board had moved to react-select, and the suite stayed
green while the adapter misread every dropdown. Multiply that by three thousand
sites and it is not a maintenance burden, it is the whole project.

Nothing here fetches. `PoliteFetcher` gets the bytes — it is what enforces
robots.txt and the per-host floor (§2.6), and an extractor that fetched for
itself would route around both.

## What it does not do

It reads what the page already publishes. A page with no JSON-LD yields
nothing, and that is the honest answer rather than a reason to start guessing
at markup. The count of pages that yield nothing is the measurement that says
whether a second strategy is worth building.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import structlog

from packages.crawler.extract import (
    ExtractedPosting,
    parse_timestamp,
    posting_hash,
    strip_html,
)

log = structlog.get_logger(__name__)

ATS = "jsonld"

#: schema.org types we read. `JobPosting` is the one that matters; the others
#: appear as wrappers a site puts around it.
_POSTING_TYPE = "jobposting"
_GRAPH_KEYS = ("@graph", "itemListElement", "mainEntity")


class _ScriptCollector(HTMLParser):
    """Pull the body of every `<script type="application/ld+json">`.

    The stdlib tokenizer rather than a regex for the reason `_TextExtractor`
    gives next door: a regex cannot tell it is inside a comment or an attribute,
    and JSON-LD blocks routinely contain `</` inside string values.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self._capturing = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        kind = next((v or "" for k, v in attrs if k.lower() == "type"), "")
        self._capturing = kind.strip().lower() == "application/ld+json"

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._capturing = False

    def handle_data(self, data: str) -> None:
        if self._capturing and data.strip():
            self.blocks.append(data)


def script_blocks(html: str) -> list[str]:
    """Every `application/ld+json` payload on the page, in document order."""
    collector = _ScriptCollector()
    try:
        collector.feed(html)
        collector.close()
    except Exception as exc:  # noqa: BLE001 — malformed markup is normal
        log.debug("jsonld_parse_failed", error=type(exc).__name__)
    return collector.blocks


def _types_of(node: dict[str, Any]) -> set[str]:
    """Every `@type` on a node, lowercased. The field is a string or a list."""
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return {str(v).strip().lower() for v in values if v}


def _walk(node: Any, found: list[dict[str, Any]]) -> None:
    """Collect every JobPosting object, however deeply it is wrapped.

    Sites nest these inconsistently — bare, inside `@graph`, inside an
    `ItemList`, or as a list at the top level. Walking is cheaper than
    enumerating the shapes, and a page that invents a new wrapper still works.
    """
    if isinstance(node, list):
        for item in node:
            _walk(item, found)
        return
    if not isinstance(node, dict):
        return

    if _POSTING_TYPE in _types_of(node):
        found.append(node)
        # Do not descend: a JobPosting's own fields are not more JobPostings,
        # and `hiringOrganization` can carry one in badly built markup.
        return

    for key in _GRAPH_KEYS:
        if key in node:
            _walk(node[key], found)


def job_postings(html: str) -> list[dict[str, Any]]:
    """Every schema.org JobPosting object on the page.

    One malformed block must not lose the others. Sites commonly emit several,
    and a trailing comma in the analytics one should not cost us the jobs.

    `RecursionError` is caught beside the decode error because both `json.loads`
    and `_walk` recurse, and the json module sets no nesting limit of its own —
    it inherits the interpreter's. Deeply nested JSON-LD is therefore a page
    that raises rather than parses, and `bespoke.probe_page` calls `extract`
    outside its own try, so one such page would end a sweep of thousands.
    """
    found: list[dict[str, Any]] = []
    for block in script_blocks(html):
        try:
            payload = json.loads(block)
            # Inside the try as well: a structure shallow enough for the
            # decoder can still be deep enough for the walk.
            _walk(payload, found)
        except json.JSONDecodeError:
            log.debug("jsonld_block_not_json")
        except RecursionError:
            # Whatever `_walk` appended before it ran out of stack is kept:
            # partial is better than losing the page, and each appended node
            # is a complete object.
            log.debug("jsonld_block_too_deeply_nested")
    return found


def _text(value: Any) -> str | None:
    """A trimmed string, or None for anything that is not one.

    The normaliser every open-record field goes through. These objects are
    arbitrary JSON from sites we do not control, so a field the schema calls
    a string routinely arrives as a dict, a list or a number. Returning None
    rather than raising is what lets one odd field cost one field.
    """
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def _location_of(node: dict[str, Any]) -> str | None:
    """A readable location, from whichever shape the site used.

    `jobLocation` may be a Place, a list of them, or a bare string, and the
    address inside is a PostalAddress whose fields are all optional. Joined
    city-region-country because `locality.py` reads exactly that — a bare
    "London" is ambiguous where "London, United Kingdom" is not.
    """
    raw = node.get("jobLocation")
    if isinstance(raw, list):
        parts = [_location_of({"jobLocation": item}) for item in raw]
        named = [p for p in parts if p]
        return "; ".join(dict.fromkeys(named)) or None

    if isinstance(raw, str):
        return _text(raw)
    if not isinstance(raw, dict):
        return _remote_location(node)

    address = raw.get("address")
    if isinstance(address, str):
        return _text(address)
    if not isinstance(address, dict):
        return _text(raw.get("name")) or _remote_location(node)

    # `addressCountry` is a string in most markup and a Country object in some.
    # Bound to a local and type-checked rather than guarded with `or {}`: that
    # guard only catches falsy values, so a list — which sites do emit — reached
    # `.get` and raised AttributeError out of the whole extraction.
    country = address.get("addressCountry")
    pieces = [
        _text(address.get("addressLocality")),
        _text(address.get("addressRegion")),
        _text(country.get("name")) if isinstance(country, dict) else _text(country),
    ]
    joined = ", ".join(p for p in pieces if p)
    return joined or _text(raw.get("name")) or _remote_location(node)


def _remote_location(node: dict[str, Any]) -> str | None:
    """`jobLocationType: TELECOMMUTE` is how the schema says "remote".

    Worth reading rather than leaving the location empty: an empty location
    classifies as UNKNOWN, while "Remote" plus whatever region the site gave
    is what the search area actually needs to decide.
    """
    if str(node.get("jobLocationType") or "").strip().upper() != "TELECOMMUTE":
        return None
    requirement = node.get("applicantLocationRequirements")
    names: list[str] = []
    for item in requirement if isinstance(requirement, list) else [requirement]:
        name = _text(item.get("name")) if isinstance(item, dict) else _text(item)
        if name:
            names.append(name)
    return f"Remote - {', '.join(dict.fromkeys(names))}" if names else "Remote"


_ID_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _external_id(node: dict[str, Any], url: str | None, title: str | None) -> str | None:
    """The site's own identifier, or a stable stand-in derived from the URL.

    Never a hash of the *description*: a posting whose text is edited would
    change id and re-appear as a new job. The URL is what a person would call
    the same posting, so it is what identity is built on.
    """
    for key in ("identifier", "jobPostingId", "@id"):
        raw = node.get(key)
        if isinstance(raw, dict):
            raw = raw.get("value") or raw.get("name")
        if isinstance(raw, str | int) and str(raw).strip():
            return _ID_SAFE.sub("-", str(raw).strip())[:120]
    if url:
        return _ID_SAFE.sub("-", url.strip())[:120]
    if title:
        return _ID_SAFE.sub("-", title.strip().lower())[:120]
    return None


def _published(node: dict[str, Any]) -> datetime | None:
    """When the site says the posting went up, or None when it does not say."""
    return parse_timestamp(node.get("datePosted") or node.get("dateCreated"))


def to_posting(node: dict[str, Any], *, page_url: str) -> ExtractedPosting | None:
    """One JobPosting object as an `ExtractedPosting`, or None if unusable.

    A posting with no title is refused. Everything downstream — the match
    feed, the scorer, the review screen — names a job by its title, and a row
    that cannot be named is worse than an absent one.
    """
    title = _text(node.get("title")) or _text(node.get("name"))
    if not title:
        return None

    # Resolved against the page, because a site is as likely to publish
    # "/careers/req-4471" as the absolute form and a relative URL is not
    # something an applier can open later.
    raw_url = _text(node.get("url")) or _text(node.get("sameAs"))
    url = urljoin(page_url, raw_url) if raw_url else page_url
    external_id = _external_id(node, url, title)
    if not external_id:
        return None

    posting = ExtractedPosting(
        external_id=external_id,
        url=url,
        title=title,
        location=_location_of(node),
        # Through `_text` first: `strip_html` hands its argument to
        # `HTMLParser.feed`, which raises TypeError on a dict or a list. Sites
        # do emit both, and the exception escaped `extract` entirely.
        description_raw=strip_html(_text(node.get("description"))),
        ats_type=ATS,
        published_at=_published(node),
    )
    posting.content_hash = posting_hash(posting)
    return posting


def extract(html: str, *, page_url: str) -> list[ExtractedPosting]:
    """Every readable JobPosting on one page. Empty when the page has none.

    One unreadable posting never costs the others, and never costs the sweep.
    The fields above are normalised for the shapes sites are known to emit, but
    this is arbitrary JSON from three thousand strangers' sites and the next
    shape is one nobody has seen yet. `job_postings` already refuses to let one
    malformed *block* lose a page; this is the same rule one level down, and it
    is what makes `bespoke.probe_page`'s "never raises" true rather than
    aspirational — a single odd page would otherwise end a sweep of thousands.
    """
    postings: list[ExtractedPosting] = []
    seen: set[str] = set()
    for node in job_postings(html):
        try:
            posting = to_posting(node, page_url=page_url)
        except Exception as exc:  # noqa: BLE001 — arbitrary third-party JSON
            log.debug("jsonld_posting_unreadable", error=type(exc).__name__)
            continue
        if posting is None or posting.external_id in seen:
            continue
        seen.add(posting.external_id)
        postings.append(posting)
    return postings


class JsonLdExtractor:
    """A bespoke careers page, read through the structured data it publishes.

    The four board APIs build their URL from a company slug. A bespoke page has
    no such rule — it is at whatever address the company chose — so **a
    `jsonld` seed's slug is that URL**, and `board_url` returns it unchanged.

    Carrying the address in `slug` rather than special-casing the crawler keeps
    `(ats, slug)` the identity key it already is in `discover.py`,
    `import_companies.py` and `import_portals.py`: two seeds for the same page
    collide, which is what de-duplication wants. `careers_url` on the seed
    carries the same string so the YAML reads as a careers page rather than as
    a riddle.
    """

    ats = ATS

    def board_url(self, company_slug: str) -> str:
        """The page itself — a bespoke seed's slug is its URL."""
        if not company_slug.lower().startswith(("http://", "https://")):
            # Not raised: `crawl_company` calls this before its try block, so a
            # raise here would end the whole cycle over one bad row. Returned
            # as-is, the fetch fails and only this company is reported.
            log.warning("jsonld_seed_slug_is_not_a_url", slug=company_slug)
        return company_slug

    def parse(self, body: str, company_slug: str) -> list[ExtractedPosting]:
        """Read the fetched page. The slug is the page URL, so it is the base
        a relative posting link resolves against."""
        return extract(body, page_url=company_slug)
