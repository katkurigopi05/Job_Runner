"""A harder look at a company a worker noted, once nothing else is waiting.

Discovery reads a company's home page and one careers page, and a crawl reads
a board. When either comes back with nothing, the company is noted
(`issues.py`) and the worker moves on. This is the second look, taken once
per note: a few more requests to the company's own site, chosen by what the
first look saw, and a sentence for whoever reads the error list.

The steps are the ones that found something when tried by hand. On
2026-10-19, 53 companies discovery had just failed were read again more
deeply, read-only:

- **unreachable (17).** Two answered at the other form of their address
  (`www.` added or dropped), three had no DNS record at all, and the rest
  refused or failed on both forms.
- **a careers page naming no board (21).** One link further in found Workday
  (Boeing) and Rise People (CarbonRun); two take applications by email; two
  list jobs on their own pages; ten showed nothing without running the page's
  JavaScript, which nothing here does yet.
- **no careers page (15).** More of the usual addresses and the sitemap found
  nothing on any of them. One linked its parent company's careers page.

So that is what is tried, and nothing else. Every request goes through the
polite fetcher: robots.txt and the 60s floor per host hold as they do for
discovery (§2.5, §2.6). A link to another site is named, never followed: a
board found there could be anyone's (`find_boards`, GGV Capital).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from packages.crawler.fetch import Blocked, PoliteFetcher
from packages.crawler.find_boards import (
    Resolved,
    SiteReading,
    names_a_board,
    resolve_site,
    site_of,
    unreadable_reason,
)
from packages.crawler.job_systems import other_job_system, system_for_host

Resolver = Callable[[str], Awaitable[bool]]

#: What a person should do, by the category a worker noted, when the deep
#: dive has nothing to add: the page named a system we cannot read, the site
#: refused, and so on. Kept beside the steps rather than imported from the
#: report, so a fix reads the same in both places.
NOTHING_TO_TRY: dict[str, str] = {
    "other_job_system": "add a reader for that job system",
    "structured_jobs": "read its careers page's structured jobs (the JSON-LD reader)",
    "multi_company_board": "add its own board URL to the sheet if it has one",
    "no_website": "add a website or careers URL to the sheet",
    "blocked": "none: its site disallows crawling (robots.txt)",
    "board_blocked": "none: the board disallows crawling (robots.txt)",
}

#: Words in a link that point at jobs. Matched on the decoded address.
_JOB_LINK_RE = re.compile(
    r"job|career|hiring|opening|position|vacanc|join|apply|work-with|recruit", re.I
)
#: Files, not pages.
_FILE_RE = re.compile(
    r"\.(?:css|js|mjs|json|xml|md|txt|png|jpe?g|gif|svg|webp|avif|ico|woff2?|ttf|pdf|"
    r"docx?|zip|mp4|webm)$",
    re.I,
)
#: Addresses that are a page's plumbing rather than a link anyone follows.
_PLUMBING_RE = re.compile(r"/wp-json/|/oembed|/feed/?$|/xmlrpc|/cdn-cgi/", re.I)
#: Sites that link everywhere and hire through none of it.
_IGNORED_SITES = frozenset(
    {
        "facebook.com",
        "twitter.com",
        "x.com",
        "instagram.com",
        "youtube.com",
        "tiktok.com",
        "slack.com",
        "google.com",
        "apple.com",
        "medium.com",
        "github.com",
        "wa.me",
        "whatsapp.com",
    }
)
#: One posting on the company's own site: a careers or jobs path, then a slug.
_OWN_POSTING_RE = re.compile(r"/(?:careers?|jobs?|positions?|openings?)/[a-z0-9][\w-]{2,}/?$", re.I)
#: An address applications are sent to.
_MAILTO_RE = re.compile(r"""href=["']mailto:([^"'?]+)(\?[^"']*)?["']""", re.I)
_APPLY_MAIL_RE = re.compile(r"career|job|hr\b|^hr|talent|recruit|people|hiring|apply|work", re.I)
_HREF_RE = re.compile(r"""href=["']([^"'#]+)["']""", re.I)


@dataclass(frozen=True)
class DeepDive:
    """What the deep dive found: a board, or why there is none and what to do."""

    board: Resolved | None = None
    diagnosis: str = ""
    fix: str = ""
    findings: dict[str, Any] = field(default_factory=dict)
    #: The finding in one word, for counting the error list by what is wrong.
    kind: str = ""


@dataclass(frozen=True)
class _Page:
    """What a page links to that matters here."""

    url: str
    own_postings: tuple[str, ...] = ()
    own_job_links: tuple[str, ...] = ()
    other_sites: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()


async def resolves(host: str) -> bool:
    """Whether a host has an address at all. No request is made to it."""
    try:
        await asyncio.get_running_loop().getaddrinfo(host, 443)
    except OSError:
        return False
    return True


def _without_guessing(reason: str) -> str:
    return reason.split("; name-guessing is off")[0]


def read_links(html: str, url: str) -> _Page:
    """Sort a page's job links into its own postings, its own job pages,
    other sites, and application email addresses."""
    own_site = site_of(urlparse(url).hostname or "")
    postings: list[str] = []
    pages: list[str] = []
    others: list[str] = []
    for href in _HREF_RE.findall(html):
        link = urljoin(url, href.strip())
        parsed = urlparse(link)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            continue
        path = unquote(parsed.path)
        if _FILE_RE.search(path) or _PLUMBING_RE.search(link):
            continue
        if not _JOB_LINK_RE.search(f"{parsed.hostname}{path}"):
            continue
        if link.rstrip("/") == url.rstrip("/"):
            continue
        site = site_of(parsed.hostname)
        if site == own_site:
            target = postings if _OWN_POSTING_RE.search(path) else pages
        elif site in _IGNORED_SITES:
            continue
        else:
            target = others
        if link not in target:
            target.append(link)
    emails = [
        address
        for address, subject in _MAILTO_RE.findall(html)
        if _APPLY_MAIL_RE.search(address.split("@")[0]) or _APPLY_MAIL_RE.search(subject or "")
    ]
    return _Page(
        url=url,
        own_postings=tuple(postings),
        own_job_links=tuple(pages),
        other_sites=tuple(others),
        emails=tuple(dict.fromkeys(emails)),
    )


async def _fetch(fetcher: PoliteFetcher, url: str) -> tuple[str, str, str]:
    """(status, final url, body). The status is a code, `blocked`, or an error type."""
    try:
        response = await fetcher.fetch(url)
    except Blocked:
        return "blocked", url, ""
    except Exception as exc:  # noqa: BLE001 - one bad site must not stop the deep dive
        return type(exc).__name__, url, ""
    return str(response.status), response.url, response.text if response.ok else ""


def _merged(first: _Page, *more: _Page) -> _Page:
    """One page's links followed by another's, each kind in order, once."""

    def joined(kind: str) -> tuple[str, ...]:
        links = [link for page in (first, *more) for link in getattr(page, kind)]
        return tuple(dict.fromkeys(links))

    return _Page(
        url=first.url,
        own_postings=joined("own_postings"),
        own_job_links=joined("own_job_links"),
        other_sites=joined("other_sites"),
        emails=joined("emails"),
    )


def _from_links(page: _Page, findings: dict[str, Any]) -> DeepDive | None:
    """A diagnosis from what a page links to, without another request."""
    if page.other_sites:
        link = page.other_sites[0]
        host = urlparse(link).hostname or ""
        system = system_for_host(host)
        findings["other_sites"] = list(page.other_sites[:5])
        if system:
            return DeepDive(
                kind="other_job_system",
                diagnosis=f"its careers page sends applicants to {system} ({link})",
                fix=f"add a reader for {system}",
                findings=findings,
            )
        return DeepDive(
            kind="sends_elsewhere",
            diagnosis=f"its careers page sends applicants to {host} ({link})",
            fix=f"look at {host}: a job system we do not read yet, or a parent company's "
            "careers page",
            findings=findings,
        )
    if len(page.own_postings) >= 2:
        findings["own_postings"] = list(page.own_postings[:5])
        return DeepDive(
            kind="own_job_pages",
            diagnosis=f"its careers page lists {len(page.own_postings)} jobs on its own pages "
            f"({page.own_postings[0]}, ...), not on a job board",
            fix="needs a reader for its own job pages",
            findings=findings,
        )
    if page.emails:
        findings["emails"] = list(page.emails)
        return DeepDive(
            kind="by_email",
            diagnosis=f"it takes applications by email ({page.emails[0]})",
            fix="none: apply by email; there is no board to read",
            findings=findings,
        )
    return None


async def _careers_page(name: str, page_url: str, fetcher: PoliteFetcher) -> DeepDive:
    """Read the careers page again, then one link further in."""
    status, final, html = await _fetch(fetcher, page_url)
    findings: dict[str, Any] = {"careers_page": {"url": page_url, "status": status}}
    if not html:
        return DeepDive(
            kind="careers_page_gone",
            diagnosis=f"its careers page {page_url} could not be read again ({status})",
            fix="check it in a browser",
            findings=findings,
        )
    page = read_links(html, final)

    # One link further in: a jobs subdomain, an "open positions" page, or the
    # first of its own postings, whose apply button may name the board. Read
    # once; read again through discovery only when it names a board, so the
    # board is verified the way discovery verifies one.
    further: list[_Page] = []
    for link in (page.own_job_links or page.own_postings)[:1]:
        status, landed, body = await _fetch(fetcher, link)
        findings["further"] = {"url": link, "status": status}
        if not body:
            continue
        if names_a_board(body):
            outcome, reading = await resolve_site(name, fetcher, url=link, guess=False)
            findings["further"]["category"] = reading.category
            if isinstance(outcome, Resolved):
                return DeepDive(
                    board=outcome,
                    kind="board_further_in",
                    diagnosis=f"its board is named one page further in, on {link}",
                    findings=findings,
                )
        system = other_job_system(body)
        if system:
            return DeepDive(
                kind="other_job_system",
                diagnosis=f"its jobs are on {system[0]} ({system[1]}), one page further in "
                "than its careers page",
                fix=f"add a reader for {system[0]}",
                findings=findings,
            )
        further.append(read_links(body, landed))

    found = _from_links(_merged(page, *further), findings)
    if found is not None:
        return found
    return DeepDive(
        kind="needs_browser",
        diagnosis=f"its careers page {final} shows no jobs, board or link to one without "
        "running its JavaScript",
        fix="look at it in a browser; reading pages built by JavaScript is not done yet",
        findings=findings,
    )


async def _home_page(lead: str, fetcher: PoliteFetcher) -> DeepDive:
    """A site with no careers page: what its home page links to instead."""
    status, final, html = await _fetch(fetcher, lead)
    findings: dict[str, Any] = {"home": {"url": lead, "status": status}}
    if html:
        found = _from_links(read_links(html, final), findings)
        if found is not None:
            return replace(
                found, diagnosis=found.diagnosis.replace("its careers page", "its site", 1)
            )
    return DeepDive(
        kind="not_hiring",
        diagnosis="its site has no careers page and links to none: it may not be hiring",
        fix="add a careers URL to the sheet if you know one; otherwise leave it",
        findings=findings,
    )


def _other_form(host: str) -> str:
    return host[4:] if host.startswith("www.") else f"www.{host}"


async def _unreachable(
    name: str, lead: str, reason: str, fetcher: PoliteFetcher, resolver: Resolver
) -> DeepDive:
    """A site that did not answer: is it gone, or at the other form of its address?"""
    parsed = urlparse(lead)
    host = (parsed.hostname or "").lower()
    other = _other_form(host)
    dns = {host: await resolver(host), other: await resolver(other)}
    findings: dict[str, Any] = {"dns": dns}
    if not any(dns.values()):
        return DeepDive(
            kind="domain_gone",
            diagnosis=f"its domain no longer exists: neither {host} nor {other} has an address",
            fix="the company may have closed; add its current website if it has one",
            findings=findings,
        )
    if dns[other]:
        address = f"{parsed.scheme or 'https'}://{other}/"
        outcome, reading = await resolve_site(name, fetcher, url=address, guess=False)
        findings["other_address"] = {"url": address, "category": reading.category}
        if isinstance(outcome, Resolved):
            return DeepDive(
                board=outcome,
                kind="other_address",
                diagnosis=f"its site answers at {other}, not {host}, and names its board there",
                findings=findings,
            )
        if reading.read:
            deeper = await _after_reading(name, address, reading, fetcher)
            return replace(
                deeper,
                kind="other_address",
                diagnosis=f"its site answers at {other}, not {host}; {deeper.diagnosis}",
                fix=f"use {address} as its website; then {deeper.fix}",
                findings={**findings, **deeper.findings},
            )
        findings["other_address"]["failed"] = reading.blocked or reading.unreachable
    return DeepDive(
        kind="refuses",
        diagnosis=f"its site does not answer us at {host} or {other} ({_without_guessing(reason)})",
        fix="check it in a browser; a site that refuses automated reading is left alone (§2.5)",
        findings=findings,
    )


async def _after_reading(
    name: str, lead: str, reading: SiteReading, fetcher: PoliteFetcher
) -> DeepDive:
    """The step for what a site's reading showed."""
    if reading.careers_page and reading.category == "careers_page_unreadable":
        return await _careers_page(name, reading.careers_page, fetcher)
    if reading.category == "no_careers_page":
        return await _home_page(lead, fetcher)
    return DeepDive(
        kind=reading.category,
        diagnosis=unreadable_reason(reading),
        fix=NOTHING_TO_TRY.get(reading.category, "check it by hand"),
    )


async def _board_stopped(
    name: str,
    lead: str | None,
    category: str,
    reason: str,
    board: tuple[str, str] | None,
    fetcher: PoliteFetcher,
) -> DeepDive:
    """A known board that keeps failing: has the company moved to another?"""
    named = f"{board[0]}/{board[1]}" if board else "its board"
    if category == "board_reads_empty":
        return DeepDive(
            kind="reader_out_of_date",
            diagnosis=f"{named} answers, but our reader finds no jobs on it while we hold "
            "open ones: the reader is probably out of date",
            fix=f"check the {board[0] + ' ' if board else ''}reader against the board",
        )
    if category == "board_blocked":
        return DeepDive(
            kind="board_blocked",
            diagnosis=f"{named}: {reason}",
            fix=NOTHING_TO_TRY["board_blocked"],
        )
    what = "no longer exists" if category == "board_gone" else f"keeps failing ({reason})"
    if not lead:
        return DeepDive(
            kind="board_gone",
            diagnosis=f"{named} {what}, and there is no website to look for another",
            fix="add its new board's URL if it still hires; otherwise it can be retired",
        )
    outcome, reading = await resolve_site(name, fetcher, url=lead, guess=False)
    findings: dict[str, Any] = {"site": {"url": lead, "category": reading.category}}
    if isinstance(outcome, Resolved):
        moved = (outcome.ats, outcome.slug) != board
        return DeepDive(
            board=outcome,
            kind="board_moved" if moved else "board_back",
            diagnosis=(
                f"{named} {what}; its site now names {outcome.ats}/{outcome.slug}"
                if moved
                else f"{named} answers again"
            ),
            findings=findings,
        )
    return DeepDive(
        kind="board_gone",
        diagnosis=f"{named} {what}, and its site names no other: {unreadable_reason(reading)}",
        fix="add its new board's URL if it still hires; otherwise it can be retired",
        findings=findings,
    )


async def deep_dive(
    *,
    name: str,
    stage: str,
    category: str,
    reason: str,
    lead: str | None,
    fetcher: PoliteFetcher,
    careers_page: str | None = None,
    board: tuple[str, str] | None = None,
    resolver: Resolver = resolves,
) -> DeepDive:
    """Look harder at one noted company. Never raises for a site that misbehaves."""
    if stage == "crawl":
        return await _board_stopped(name, lead, category, reason, board, fetcher)
    if category == "unreachable" and lead:
        return await _unreachable(name, lead, reason, fetcher, resolver)
    if category == "careers_page_unreadable" and careers_page:
        return await _careers_page(name, careers_page, fetcher)
    if category == "no_careers_page" and lead:
        return await _home_page(lead, fetcher)
    return DeepDive(
        kind=category,
        diagnosis=_without_guessing(reason),
        fix=NOTHING_TO_TRY.get(category, "check it by hand"),
        findings={"steps": "none: nothing more to try for this category"},
    )


__all__ = ["NOTHING_TO_TRY", "DeepDive", "deep_dive", "read_links", "resolves"]
