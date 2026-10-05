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
from packages.crawler.extract import ExtractedPosting
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
    #: The directory's name for the board. For a name match it is the name the
    #: match was made on, which `confirms` holds the website's label against.
    company: str = ""


def normalize_name(name: str) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).split()
    while words and words[-1] in _SUFFIXES:
        words.pop()
    return "".join(words)


def _registrable_labels(url: str | None) -> list[str]:
    """The labels of a URL's registrable domain: `jobs.acme.co.uk` → acme, co, uk."""
    if not url:
        return []
    host = urlparse(url if "://" in url else f"https://{url}").hostname or ""
    labels = [label for label in host.lower().removeprefix("www.").split(".") if label]
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return labels[-3:]
    return labels[-2:]


def domain_stem(url: str | None) -> str:
    """The registrable label of a URL's host: `https://jobs.acme.co.uk` → `acme`."""
    labels = _registrable_labels(url)
    return labels[0] if labels else ""


def registrable_domain(url: str | None) -> str:
    """A URL's registrable domain: `https://usa.baidu.com/x` → `baidu.com`."""
    return ".".join(_registrable_labels(url))


def confirms(
    candidate: Candidate, website: str | None, postings: Iterable[ExtractedPosting]
) -> str | None:
    """Why this board is the company's own, or None if nothing says it is.

    A name is shared and a domain is not. The 2026-10-05 benchmark verified
    four namesakes from the directories — Artemis (artemispower.com) received
    an AI-security startup's board, Axle (axlepayments.com) Axle Informatics',
    Arena (arena.im) LMArena's — and in every one the evidence was a name, or a
    label that some other company owns under a different TLD. So a board
    counts when:

    - its slug is the website's own `.com` label. Whoever holds `acme.com`
      owns the name `acme`; a site on another TLD often chose it because the
      `.com` belonged to someone else, which is exactly how arena.im lost.
    - or its postings name the website's domain, in their text or in their
      own URLs — Greenhouse returns the employer's careers URL when set.

    - or, for a match made on the name, the website's label agrees with that
      name. The label is then an independent witness: `artemispower`,
      `axlepayments` and `beamsolutions` each say the company's name is longer
      than the one that matched, and all three boards were namesakes, while
      `tryascend`, `trybadge`, `arkham` and `better` agreed and were right. A
      match made on the label gets no such pass — the label cannot vouch for
      a match it made, which is how arena.im got LMArena's board.

    With no website there is nothing to check a name against, and the
    resolver already accepts a name guess on the same terms, so a name match
    stands.
    """
    domain = registrable_domain(website)
    if not domain:
        return "no website to check against"
    if candidate.why == "slug matches website" and domain == f"{candidate.slug.lower()}.com":
        return "slug is the website's .com name"
    mention = re.compile(rf"(?<![a-z0-9-]){re.escape(domain)}(?![a-z0-9-])")
    for posting in postings:
        text = f"{posting.url} {posting.description_raw or ''}".lower()
        if mention.search(text):
            return "board names the website"
    if candidate.why == "same name in directory" and _label_agrees(
        domain_stem(website), normalize_name(candidate.company)
    ):
        return "website agrees with the name"
    return None


#: Words a company puts around its name in a domain when the bare name was
#: taken: getcensus, withpersona, frontapp, ironcladapp. They do not change
#: whose name it is, unlike `power` in artemispower.
_LABEL_PREFIXES = ("try", "get", "join", "use", "hello", "with", "meet", "go", "hey")
_LABEL_SUFFIXES = ("app", "hq", "inc", "labs", "lab", "ai", "io", "tech", "team", "careers", "jobs")


def _label_agrees(label: str, name: str) -> bool:
    """Whether a domain label names the same company as `name`, both normalised.

    Agrees when equal once a conventional prefix or suffix is set aside, or
    when the label is the start of the name (`better` for Better Mortgage,
    `baidu` for Baidu USA). Disagrees when the label adds words of its own.
    """
    label = re.sub(r"[^a-z0-9]", "", label.lower())
    if not label or not name:
        return False
    if len(label) >= 3 and name.startswith(label):
        return True
    cores = {label}
    for prefix in _LABEL_PREFIXES:
        if label.startswith(prefix):
            cores.add(label[len(prefix) :])
    for core in list(cores):
        for suffix in _LABEL_SUFFIXES:
            if core.endswith(suffix):
                cores.add(core[: -len(suffix)])
    return name in cores


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
                found.append(
                    Candidate(vendor=entry.vendor, slug=entry.slug, why=why, company=entry.company)
                )

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
    "confirms",
    "directory_root",
    "domain_stem",
    "load_default",
    "normalize_name",
    "registrable_domain",
]
