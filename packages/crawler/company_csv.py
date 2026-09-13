"""Read a CSV of companies and careers URLs, and sort it into what we can use.

The owner has ~3,000 companies with careers URLs. `scripts/import_portals.py`
already says why they cannot simply be added to the registry:

    A company whose careers page is its own site (`twilio.com/careers`) is
    reported and skipped: we have no extractor for a bespoke page, so adding it
    to the registry would mean a crawl cycle that fetches and parses nothing
    every hour, forever.

So the first question is not "how do we scrape 3,000 sites" but **how many of
them are actually bespoke**. Careers URLs very often redirect to, or are
already, a Greenhouse/Lever/Ashby/Workable board — and every one of those needs
no new code at all. It is a registry row the existing crawler polls first-hand.

This does that sort, offline. No network, so it runs on 3,000 rows in seconds
and its answer does not depend on which sites happen to be up.

## Three outcomes, and why the third is a file rather than a number

- **promotable** — the URL names a board we can already crawl and apply to.
- **bespoke** — a real URL, on the company's own site. These are the work
  queue for a generic extractor, so they are written out rather than counted
  and dropped. A number tells you the size of the problem; the list is what
  you point the next tool at.
- **unusable** — no URL, or not http(s). Reported separately from bespoke,
  because "you have no URL for this company" and "we cannot read this page
  yet" need different fixes.

Nothing here writes to the registry. `scripts/import_companies.py` does that,
through `import_portals.append_to_registry`, so there is one writer.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from packages.crawler.find_boards import SLUG_RE, board_root, normalize_header

#: Column names seen in the wild, lowercased. The first match wins, so the
#: more specific spellings come first — a sheet with both `careers_url` and
#: `website` means the second is the marketing site.
NAME_COLUMNS = ("company_name", "company", "name", "employer", "organisation", "organization")

#: The company's *own* site. A discovery starting point, never a board on its
#: own — `acme.com` says who, not where the jobs are.
#:
#: Separate from the careers columns because the two have different roles and
#: a sheet carrying both is the common case. Picking one column for both, which
#: is what this module did, made `company_url` unreadable: the owner's
#: 3,869-row sheet has `company_name,company_url,company_career_url` and
#: matched none of the careers spellings, so `read_rows` refused the file
#: outright and `read_companies` extracted 0 URLs from 3,869 rows.
WEBSITE_COLUMNS = (
    "company_url",
    "company_website",
    "company_site",
    "website_url",
    "website",
    "homepage",
    "domain",
    "site",
)
#: Ordered by preference: a column that names careers explicitly beats a
#: generic `url`, because a sheet often carries both and the generic one is
#: the company's home page.
URL_COLUMNS = (
    "company_career_url",
    "company_careers_url",
    "careers_url",
    "career_url",
    "careers_page",
    "career_page",
    "jobs_careers_url",
    "careers_jobs_url",
    "jobs_url",
    "job_url",
    "jobs",
    "careers",
    "url",
    "link",
    "website_url",
    "website",
)

#: Hosts whose *search results* arrive in spreadsheets in place of a real
#: careers page. Membership alone decides nothing — see `is_search_url`.
SEARCH_HOSTS: frozenset[str] = frozenset(
    {
        "google.com",
        "www.google.com",
        "bing.com",
        "www.bing.com",
        "duckduckgo.com",
        "www.duckduckgo.com",
        "search.brave.com",
        "ecosia.org",
        "startpage.com",
    }
)


def is_search_url(url: str) -> bool:
    """Whether this URL is a *search result*, judged on structure not host.

    `find_boards.usable_url` answers a related question by hostname, and that
    is correct for its caller — it is asked "could this name a board", and a
    search engine cannot. Asked instead "is this a company", the hostname test
    gives the wrong answer for four real employers in the owner's own sheet:

        https://google.com          -> rejected, but Google is a company
        https://cloud.google.com    -> rejected, but Google Cloud is a company
        https://firebase.google.com -> rejected, but Firebase is a company
        https://linkedin.com        -> rejected, but LinkedIn is a company

    So the test is the *shape*: a search engine's host **and** a search path or
    a query string. `google.com` is a homepage; `google.com/search?q=...` is a
    search. Both are on the same host and only one of them is a lead.
    """
    cleaned = url.strip()
    if not cleaned:
        return False
    try:
        parsed = urlparse(cleaned)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if host not in SEARCH_HOSTS:
        return False
    # A bare host, or a host with a trivial path, is that company's home page.
    path = parsed.path.rstrip("/").lower()
    return bool(parsed.query) or path.startswith(("/search", "/url", "/maps", "/imgres"))


#: A board link that points at one posting rather than the board root —
#: `boards.greenhouse.io/acme/jobs/12345`. `board_root` deliberately anchors to
#: the root, so this is the second chance: the slug is still right there, and a
#: CSV assembled by hand is full of these.
_DEEP_LINK_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "greenhouse",
        re.compile(
            r"^https?://(?:www\.)?(?:job-)?boards(?:\.eu)?\.greenhouse\.io/"
            r"(?P<slug>[A-Za-z0-9._-]+)(?:/|$)",
            re.I,
        ),
    ),
    (
        "lever",
        re.compile(r"^https?://jobs\.(?:eu\.)?lever\.co/(?P<slug>[A-Za-z0-9._-]+)(?:/|$)", re.I),
    ),
    (
        "ashby",
        re.compile(r"^https?://jobs\.ashbyhq\.com/(?P<slug>[A-Za-z0-9._-]+)(?:/|$)", re.I),
    ),
    (
        "workable",
        re.compile(
            r"^https?://(?:apply|jobs)\.workable\.com/(?P<slug>[A-Za-z0-9._-]+)(?:/|$)", re.I
        ),
    ),
)


@dataclass(frozen=True)
class Row:
    """One CSV line, normalised."""

    name: str
    #: The best board *candidate* the row offers: the careers URL when it is
    #: one we could read, otherwise the website. Kept as the first field so
    #: every existing caller and test reads unchanged.
    url: str
    #: The company's own site, from a website-role column. A discovery
    #: starting point; never a board on its own.
    website: str = ""
    #: Whatever the careers column actually said, preserved verbatim even when
    #: it is a search link. A hint is not a career source, and the row keeps it
    #: so a later tool can see what the sheet claimed.
    career_hint: str = ""
    #: Every column, kept so a caller can write the bespoke list back out with
    #: whatever the sheet carried — a contact, a sector, a headcount.
    raw: dict[str, str] = field(default_factory=dict)
    #: 1-based line in the source file, header excluded. Provenance: a
    #: duplicate name three thousand rows in is otherwise unfindable.
    source_row: int = 0


@dataclass(frozen=True)
class Classified:
    row: Row
    #: "greenhouse" | "lever" | "ashby" | "workable" for a promotable row.
    ats: str | None = None
    slug: str | None = None
    reason: str = ""

    @property
    def promotable(self) -> bool:
        return bool(self.ats and self.slug)


@dataclass
class TriageReport:
    total: int = 0
    promotable: list[Classified] = field(default_factory=list)
    bespoke: list[Classified] = field(default_factory=list)
    #: Rows whose only lead is the company's own site. Discovery work, not
    #: crawl work and not a dead end.
    #:
    #: Before this bucket existed the two ends of the sheet collapsed into the
    #: wrong answers: a row with a website and a search link in the careers
    #: column was `unusable`, which is what 3,864 of the owner's 3,869 rows
    #: are. They are not unusable — `find_boards` resolves a board from a
    #: company's own domain, and the website is exactly the evidence it wants.
    #: Filing them as `bespoke` would be the other mistake, because
    #: `probe-bespoke` would fetch a home page hunting for JobPosting data that
    #: belongs on a careers page.
    candidates: list[Classified] = field(default_factory=list)
    unusable: list[Classified] = field(default_factory=list)
    duplicates: int = 0

    @property
    def by_vendor(self) -> Counter[str]:
        return Counter(entry.ats or "?" for entry in self.promotable)

    def summary(self) -> str:
        lines = [f"{self.total} rows"]
        if self.duplicates:
            lines.append(f"  {self.duplicates} duplicate URLs collapsed")
        lines.append(f"  {len(self.promotable)} already a board we can crawl")
        for vendor, count in sorted(self.by_vendor.items(), key=lambda kv: -kv[1]):
            lines.append(f"      {vendor:12} {count}")
        lines.append(f"  {len(self.bespoke)} bespoke careers pages — need a generic extractor")
        lines.append(f"  {len(self.candidates)} websites only — discovery has to find the board")
        lines.append(f"  {len(self.unusable)} unusable (no lead at all)")
        return "\n".join(lines)


def _pick(header: list[str], candidates: tuple[str, ...]) -> str | None:
    """The header matching the first candidate that appears, or None.

    Normalised through `find_boards.normalize_header` rather than a second
    copy of the same regex: a real sheet spells the column `Jobs/Careers URL`,
    which merely lowercased is `jobs/careers url` and matches nothing — a
    3,802-row file was refused outright for want of a slash. Two normalisers
    would drift, and this one is already the tested one.
    """
    slugged = {normalize_header(column): column for column in header}
    for candidate in candidates:
        if candidate in slugged:
            return slugged[candidate]
    return None


def read_rows(path: Path) -> tuple[list[Row], list[str]]:
    """Parse the CSV. Returns the rows and the header, so a caller can echo it.

    Raises with the header it actually found rather than a generic parse error:
    on somebody else's 3,000-row sheet, "which column did you want" is the
    question, and guessing silently would classify everything as unusable.
    """
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames or [])
        if not header:
            raise ValueError(f"{path}: no header row")

        name_column = _pick(header, NAME_COLUMNS)
        career_column = _pick(header, URL_COLUMNS)
        website_column = _pick(header, WEBSITE_COLUMNS)
        if career_column is None and website_column is None:
            raise ValueError(
                f"{path}: no careers-URL or website column found. Looked for "
                f"{', '.join(URL_COLUMNS)} and {', '.join(WEBSITE_COLUMNS)}; "
                f"the file has {', '.join(header)}"
            )

        rows: list[Row] = []
        for number, record in enumerate(reader, start=1):
            raw = {key: (value or "").strip() for key, value in record.items() if key}
            career = raw.get(career_column, "") if career_column else ""
            website = raw.get(website_column, "") if website_column else ""
            name = raw.get(name_column, "") if name_column else ""
            # A search link in the careers column is a hint and not a
            # candidate, so it must not become `url` — that is the field every
            # board test is applied to.
            candidate = "" if is_search_url(career) else career
            rows.append(
                Row(
                    name=name or _name_from_url(candidate or website),
                    url=candidate or website,
                    website=website,
                    career_hint=career,
                    raw=raw,
                    source_row=number,
                )
            )
    return rows, header


def _name_from_url(url: str) -> str:
    """A readable stand-in when the sheet has no name column."""
    host = re.sub(r"^https?://(?:www\.)?", "", url.strip()).split("/")[0]
    return host.split(".")[0].replace("-", " ").title() if host else ""


def classify_url(url: str) -> tuple[str, str] | None:
    """`(ats, slug)` when this URL names a board we already support."""
    cleaned = url.strip()
    if not cleaned:
        return None
    found = board_root(cleaned)
    if found:
        return found
    for vendor, pattern in _DEEP_LINK_RES:
        match = pattern.match(cleaned)
        if match:
            slug = match.group("slug")
            # `embed` and `v1` are path segments, not companies.
            if SLUG_RE.match(slug) and slug.lower() not in {"embed", "v1", "jobs", "job"}:
                return vendor, slug
    return None


def triage(rows: list[Row]) -> TriageReport:
    """Sort rows by what lead they actually carry. No network.

    The routing follows the *role of the column* a URL came from, not only the
    URL's text, because the same string means different things in different
    columns: `google.com` is a company in a website column and nothing at all
    in a careers column.
    """
    report = TriageReport(total=len(rows))
    seen: set[str] = set()

    for row in rows:
        url = row.url.strip()
        website = row.website.strip()
        http = ("http://", "https://")

        # Nothing to work from. Deliberately distinguished from "we tried and
        # failed": the fix is finding a URL, not crawling better.
        if not url or not url.lower().startswith(http):
            if website and website.lower().startswith(http):
                report.candidates.append(
                    Classified(row=row, reason="website only — discovery must find the board")
                )
                continue
            if row.career_hint and is_search_url(row.career_hint):
                # Worth saying which kind of nothing this is. A sheet whose
                # careers column is a generated search link per row, with no
                # website beside it, has no lead at all — and the reason
                # explains why `probe-bespoke` must not be pointed at it.
                reason = "a search link and no website — no lead"
            elif not url:
                reason = "no URL"
            else:
                reason = "not an http(s) URL"
            report.unusable.append(Classified(row=row, reason=reason))
            continue

        key = url.rstrip("/").lower()
        if key in seen:
            report.duplicates += 1
            continue
        seen.add(key)

        found = classify_url(url)
        if found:
            # A direct board URL. Still a *candidate* board rather than a
            # verified one — `jobs.lever.co/xchg` is the right shape and may
            # still 404 — so verification happens against the board API, not
            # here.
            report.promotable.append(Classified(row=row, ats=found[0], slug=found[1]))
            continue

        # The URL is on somebody's own site. Which bucket depends on whether
        # it is a *careers* page or merely the home page: a careers page is
        # what `probe-bespoke` reads for JobPosting data, and a home page is
        # what `find_boards` reads for an embedded board.
        if url == website and row.career_hint and is_search_url(row.career_hint):
            report.candidates.append(
                Classified(row=row, reason="careers column was a search link; website is the lead")
            )
        elif url == website:
            # Either the careers column was empty, or it repeated the website.
            # The latter is how a vendor's own row arrives — `Greenhouse
            # Software` lists `greenhouse.io` in both columns — and it is a home
            # page either way, so it is a lead for discovery rather than a
            # careers page for `probe-bespoke` to read. Those four companies
            # stay eligible employers; what is refused is reading a vendor's
            # domain as a board identifier, which `classify_url` already does.
            report.candidates.append(
                Classified(row=row, reason="website only — discovery must find the board")
            )
        else:
            report.bespoke.append(
                Classified(row=row, reason="careers page is on the company's own site")
            )

    return report


def write_bespoke(entries: list[Classified], path: Path, header: list[str]) -> Path:
    """The work queue for a generic extractor, in the shape it arrived in.

    Every original column is preserved. A sheet that carried a sector or a
    headcount should not lose it on the way through here — the next tool may
    want to crawl the largest first.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = header or ["name", "url"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for entry in entries:
            writer.writerow(entry.row.raw or {"name": entry.row.name, "url": entry.row.url})
    return path
