"""Probe the bespoke careers pages `make import-csv` set aside.

`scripts/import_companies.py` sorts a sheet of companies into boards we can
already crawl and a remainder it writes to `seeds/bespoke_careers.csv`:

    These need the generic extractor — JSON-LD and sitemaps — not a new adapter.

`packages/crawler/jsonld.py` is that extractor. This is the step between them:
fetch each page once, ask whether it actually publishes `JobPosting` data, and
promote only the ones that do.

## Why a probe rather than promoting the whole file

`import_portals.py` states the rule this obeys:

    A company whose careers page is its own site (`twilio.com/careers`) is
    reported and skipped: we have no extractor for a bespoke page, so adding it
    to the registry would mean a crawl cycle that fetches and parses nothing
    every hour, forever.

Having an extractor does not repeal that. A page with no structured data is
still a row the crawler would poll forever for nothing, and — worse — a board
that yields zero postings reads exactly like a board with nothing new. So the
registry only gains the pages that answered.

## What a failure means, and what it does not

Only `PUBLISHES` promotes. A page that timed out, 403'd, or is disallowed by
robots.txt is reported and left in the file: a site that is down this afternoon
is not a site without structured data, and treating the two the same would
retire companies on the strength of one bad request.

The per-host floor (§2.6) costs nothing here. These are thousands of *distinct*
hosts, one request each, so the floor never serializes two of them behind one
counter — unlike a shared ATS API, which is why that exception exists at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import structlog

from packages.crawler.company_csv import Row, read_rows
from packages.crawler.extract import CompanySeed
from packages.crawler.fetch import Blocked, PoliteFetcher
from packages.crawler.jsonld import ATS, extract

log = structlog.get_logger(__name__)


class BespokeState(StrEnum):
    #: The page publishes schema.org JobPosting data we can read.
    PUBLISHES = "publishes"
    #: Fetched fine, no JobPosting on it. Not promotable — see the module
    #: docstring — but a real answer, unlike the two below.
    NO_DATA = "no_data"
    #: robots.txt said no. Not a verdict on the page.
    BLOCKED = "blocked"
    #: The request failed or the server refused. Also not a verdict.
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class ProbeResult:
    name: str
    url: str
    state: BespokeState
    postings: int = 0
    status: int | None = None
    #: A sample of what was found, so a verdict can be sanity-checked without
    #: a second fetch. Titles only — the descriptions are the bulk of a page.
    titles: tuple[str, ...] = ()


@dataclass
class ProbeReport:
    results: list[ProbeResult] = field(default_factory=list)

    def of(self, state: BespokeState) -> list[ProbeResult]:
        """Every result with one outcome. Each state is reported separately
        because they need different follow-ups."""
        return [r for r in self.results if r.state is state]

    @property
    def publishing(self) -> list[ProbeResult]:
        """The only results that may become registry rows."""
        return self.of(BespokeState.PUBLISHES)

    @property
    def postings(self) -> int:
        """Jobs found across the pages that published — the sweep's yield."""
        return sum(r.postings for r in self.publishing)

    def summary(self) -> str:
        """One line naming every outcome that occurred, and how many jobs the
        publishing pages carried."""
        counts = ", ".join(
            f"{len(self.of(state))} {state.value}" for state in BespokeState if self.of(state)
        )
        page = "page" if len(self.results) == 1 else "pages"
        return (
            f"{len(self.results)} {page} probed: {counts or 'nothing'} "
            f"({self.postings} postings on the {len(self.publishing)} that publish)"
        )


async def probe_page(row: Row, fetcher: PoliteFetcher) -> ProbeResult:
    """One page, fetched once. Never raises — one bad host is not the sweep."""
    name = row.name or row.url or "(unnamed)"
    url = row.url or ""
    if not url:
        return ProbeResult(name=name, url=url, state=BespokeState.UNREACHABLE)

    try:
        response = await fetcher.fetch(url)
    except Blocked as exc:
        log.info("bespoke_probe_blocked", company=name, reason=str(exc))
        return ProbeResult(name=name, url=url, state=BespokeState.BLOCKED)
    except Exception as exc:  # noqa: BLE001 — one bad host must not stop the sweep
        log.warning("bespoke_probe_failed", company=name, error=type(exc).__name__)
        return ProbeResult(name=name, url=url, state=BespokeState.UNREACHABLE)

    if not response.ok:
        return ProbeResult(
            name=name, url=url, state=BespokeState.UNREACHABLE, status=response.status
        )

    postings = extract(response.text, page_url=url)
    state = BespokeState.PUBLISHES if postings else BespokeState.NO_DATA
    return ProbeResult(
        name=name,
        url=url,
        state=state,
        postings=len(postings),
        status=response.status,
        titles=tuple(p.title for p in postings[:3] if p.title),
    )


async def probe(
    rows: list[Row], fetcher: PoliteFetcher, *, limit: int | None = None
) -> ProbeReport:
    """Probe each row in order. Sequential, because the fetcher is.

    `limit is None` rather than a truthiness test: zero is a limit, not the
    absence of one. `if limit` read `-n 0` as unlimited, so asking for a sample
    of nothing swept every page in the file — the opposite of what the flag is
    reached for, and on ~3,000 rows an afternoon rather than a moment.
    """
    report = ProbeReport()
    for row in rows if limit is None else rows[:limit]:
        report.results.append(await probe_page(row, fetcher))
    return report


def to_seeds(report: ProbeReport, existing: list[CompanySeed]) -> list[CompanySeed]:
    """Registry rows for the pages that answered, skipping what is already in.

    The seed's `slug` is the page URL, which is what `JsonLdExtractor` expects
    and what makes `(ats, slug)` — the identity key `import_companies.py` and
    `discover.py` both de-duplicate on — mean the same thing here as there.
    """
    known = {(seed.ats, seed.slug.lower()) for seed in existing}
    seeds: list[CompanySeed] = []
    for result in report.publishing:
        key = (ATS, result.url.lower())
        if key in known:
            continue
        known.add(key)
        seeds.append(
            CompanySeed(name=result.name, slug=result.url, ats=ATS, careers_url=result.url)
        )
    return seeds


def load_bespoke(path: Path) -> list[Row]:
    """The remainder file, read back in the shape `import-csv` wrote it."""
    rows, _header = read_rows(path)
    return rows
