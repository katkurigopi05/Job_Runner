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

from packages.crawler.find_boards import SLUG_RE, board_root

#: Column names seen in the wild, lowercased. The first match wins, so the
#: more specific spellings come first — a sheet with both `careers_url` and
#: `website` means the second is the marketing site.
NAME_COLUMNS = ("company_name", "company", "name", "employer", "organisation", "organization")
URL_COLUMNS = (
    "careers_url",
    "career_url",
    "careers_page",
    "career_page",
    "jobs_url",
    "job_url",
    "careers",
    "url",
    "link",
    "website",
)

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
    url: str
    #: Every column, kept so a caller can write the bespoke list back out with
    #: whatever the sheet carried — a contact, a sector, a headcount.
    raw: dict[str, str] = field(default_factory=dict)


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
        lines.append(f"  {len(self.unusable)} unusable (no URL, or not http)")
        return "\n".join(lines)


def _pick(header: list[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {column.strip().lower(): column for column in header}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
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
        url_column = _pick(header, URL_COLUMNS)
        if url_column is None:
            raise ValueError(
                f"{path}: no careers-URL column found. Looked for "
                f"{', '.join(URL_COLUMNS)}; the file has {', '.join(header)}"
            )

        rows: list[Row] = []
        for record in reader:
            raw = {key: (value or "").strip() for key, value in record.items() if key}
            url = raw.get(url_column, "")
            name = raw.get(name_column, "") if name_column else ""
            rows.append(Row(name=name or _name_from_url(url), url=url, raw=raw))
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
    """Sort rows into promotable, bespoke and unusable. No network."""
    report = TriageReport(total=len(rows))
    seen: set[str] = set()

    for row in rows:
        url = row.url.strip()
        if not url or not url.lower().startswith(("http://", "https://")):
            report.unusable.append(
                Classified(row=row, reason="no URL" if not url else "not an http(s) URL")
            )
            continue

        key = url.rstrip("/").lower()
        if key in seen:
            report.duplicates += 1
            continue
        seen.add(key)

        found = classify_url(url)
        if found:
            report.promotable.append(Classified(row=row, ats=found[0], slug=found[1]))
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
