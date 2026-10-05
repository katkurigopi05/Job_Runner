"""Public board directories: a company's board proposed without guessing.

Name-guessing probes about ten slugs per company against four shared ATS hosts
at a 2-second floor, and on the 200-company trial of the owner's sheet it found
**none** of the 188 sheet companies (docs/ATS_DISCOVERY_RESEARCH.md). Two public
directories already map thousands of companies to their boards:

- **ATS Company Directory** — Mahesh Bandaru, Figshare, CC BY 4.0
  (attribution required), 9,935 Greenhouse/Lever/Ashby boards, August 2026.
- **Job Board Directory** — free, no attribution required, 12,122 boards on
  eight platforms, four of them ours.

Offline, they proposed the exact board for 23 of the 33 the trial found. They
also proposed wrong ones, so what this module returns is a **candidate**: the
discovery handler probes it once, and a board counts only if it lists at least
one open posting, exactly as for any other route.

Two matches, strongest first, and nothing else:

- **Slug matches the website** — the board's slug equals the website's
  registrable label (`jobs.acme.com` → `acme`).
- **Same name in the directory** — the directory's company name, normalised,
  equals ours.

A generic slug (`jobs`, `careers`, …) is never proposed: in the trial join
Xpansiv was offered Ashby's `jobs`, which names no company in particular.

The files live in `storage/board_directories/` (`make fetch-board-directories`)
and are read once per process. With none present, discovery runs as before.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from packages.core.config import get_settings
from packages.crawler.find_boards import VENDORS

#: At most this many candidates are probed per company. Each is one request to
#: a shared ATS host; the directories rarely offer more than two plausible ones.
MAX_CANDIDATES = 3

#: Slugs that name a page, not a company.
GENERIC_SLUGS = frozenset(
    {
        "jobs",
        "job",
        "careers",
        "career",
        "apply",
        "hiring",
        "join",
        "joinus",
        "team",
        "work",
        "www",
        "app",
        "board",
        "boards",
        "company",
        "about",
        "home",
        "talent",
        "recruiting",
        "people",
        "hr",
        "main",
        "site",
        "test",
        "demo",
    }
)

#: Trailing words that do not tell two companies apart.
_SUFFIXES = frozenset(
    {
        "inc",
        "llc",
        "ltd",
        "corp",
        "corporation",
        "co",
        "company",
        "technologies",
        "technology",
        "tech",
        "labs",
        "lab",
        "hq",
        "group",
        "holdings",
        "the",
        "io",
    }
)

#: Second-level labels under a two-letter country code: `acme.co.uk` is Acme.
_SECOND_LEVEL = frozenset({"co", "com", "org", "net", "ac", "gov", "edu"})

#: The two published formats, by the columns that identify them.
_FORMATS = {
    "figshare": ("ats_vendor", "board_slug", "company_name"),
    "job board directory": ("platform", "token", "company"),
}


class DirectoryFormatError(ValueError):
    """A file that is neither published directory format."""


@dataclass(frozen=True)
class DirectoryEntry:
    vendor: str
    slug: str
    company: str


@dataclass(frozen=True)
class Candidate:
    vendor: str
    slug: str
    #: "slug matches website" or "same name in directory" — kept on the
    #: company's evidence, because the two are not equally strong.
    why: str


def normalize_name(name: str) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).split()
    while words and words[-1] in _SUFFIXES:
        words.pop()
    return "".join(words)


def domain_stem(url: str | None) -> str:
    """The registrable label of a URL's host: `https://jobs.acme.co.uk` → `acme`."""
    if not url:
        return ""
    host = urlparse(url if "://" in url else f"https://{url}").hostname or ""
    labels = [label for label in host.lower().removeprefix("www.").split(".") if label]
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return labels[-3]
    if len(labels) >= 2:
        return labels[-2]
    return labels[0] if labels else ""


class BoardDirectory:
    def __init__(self, entries: Iterable[DirectoryEntry]) -> None:
        self._by_slug: dict[str, list[DirectoryEntry]] = {}
        self._by_name: dict[str, list[DirectoryEntry]] = {}
        self._count = 0
        for entry in entries:
            if entry.vendor not in VENDORS or not entry.slug:
                continue
            self._count += 1
            self._by_slug.setdefault(entry.slug.lower(), []).append(entry)
            if name := normalize_name(entry.company):
                self._by_name.setdefault(name, []).append(entry)

    def __len__(self) -> int:
        return self._count

    @classmethod
    def from_files(cls, paths: Sequence[Path]) -> BoardDirectory:
        return cls(entry for path in paths for entry in _read(path))

    def candidates(self, name: str, website: str | None) -> list[Candidate]:
        found: list[Candidate] = []
        seen: set[tuple[str, str]] = set()

        def add(entries: list[DirectoryEntry], why: str) -> None:
            for entry in entries:
                key = (entry.vendor, entry.slug.lower())
                if key in seen or entry.slug.lower() in GENERIC_SLUGS:
                    continue
                seen.add(key)
                found.append(Candidate(vendor=entry.vendor, slug=entry.slug, why=why))

        if stem := domain_stem(website):
            add(self._by_slug.get(stem, []), "slug matches website")
        if normalized := normalize_name(name):
            add(self._by_name.get(normalized, []), "same name in directory")
        return found[:MAX_CANDIDATES]


def _read(path: Path) -> Iterable[DirectoryEntry]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        header = set(reader.fieldnames or [])
        for vendor_col, slug_col, company_col in _FORMATS.values():
            if {vendor_col, slug_col, company_col} <= header:
                for row in reader:
                    yield DirectoryEntry(
                        vendor=(row.get(vendor_col) or "").strip().lower(),
                        slug=(row.get(slug_col) or "").strip(),
                        company=(row.get(company_col) or "").strip(),
                    )
                return
    raise DirectoryFormatError(
        f"{path.name}: not a known board directory (header {sorted(header)})"
    )


def directory_root() -> Path:
    return Path(get_settings().storage_root) / "board_directories"


@lru_cache(maxsize=1)
def load_default() -> BoardDirectory | None:
    """The downloaded directories, read once per process, or None if absent."""
    files = sorted(directory_root().glob("*.csv"))
    return BoardDirectory.from_files(files) if files else None


__all__ = [
    "GENERIC_SLUGS",
    "MAX_CANDIDATES",
    "BoardDirectory",
    "Candidate",
    "DirectoryEntry",
    "DirectoryFormatError",
    "directory_root",
    "domain_stem",
    "load_default",
    "normalize_name",
]
