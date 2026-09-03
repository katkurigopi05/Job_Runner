"""Probing the bespoke careers pages `make import-csv` set aside.

The extractor next door reads a page. This decides which pages are worth
polling at all — and the interesting half is what it refuses to promote, since
a registry row that yields nothing reads exactly like a board with nothing new.
"""

from __future__ import annotations

import json

import httpx

from packages.crawler.bespoke import BespokeState, probe, probe_page, to_seeds
from packages.crawler.company_csv import Row
from packages.crawler.extract import CompanySeed
from packages.crawler.fetch import HostRateLimiter, PoliteFetcher
from packages.crawler.jsonld import ATS

JOB = {
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "title": "Senior Backend Engineer",
    "identifier": "REQ-1",
    "url": "https://acme.example/careers/req-1",
    "jobLocation": {"address": {"addressLocality": "San Francisco", "addressRegion": "CA"}},
}

ROBOTS_OK = "User-agent: *\nDisallow:"


def _limiter() -> HostRateLimiter:
    from tests.test_crawler import FakeClock

    clock = FakeClock()
    return HostRateLimiter(clock=clock, sleeper=clock.sleep)


def _fetcher(body: str, *, status: int = 200, robots: str = ROBOTS_OK) -> PoliteFetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        return httpx.Response(status, text=body)

    return PoliteFetcher(transport=httpx.MockTransport(handler), rate_limiter=_limiter())


def _page(*blocks: object) -> str:
    scripts = "".join(
        f'<script type="application/ld+json">{json.dumps(b)}</script>' for b in blocks
    )
    return f"<html><head>{scripts}</head><body></body></html>"


ROW = Row(name="Acme", url="https://acme.example/careers")


async def test_a_page_that_publishes_jobs_is_promotable() -> None:
    result = await probe_page(ROW, _fetcher(_page(JOB)))

    assert result.state is BespokeState.PUBLISHES
    assert result.postings == 1
    assert result.titles == ("Senior Backend Engineer",)


async def test_a_page_with_no_structured_data_is_not_promotable() -> None:
    """`import_portals.py`'s rule survives having an extractor: a page with no
    JobPosting is a crawl cycle that parses nothing every hour, forever."""
    result = await probe_page(ROW, _fetcher("<html><body><h1>Careers</h1></body></html>"))

    assert result.state is BespokeState.NO_DATA
    assert result.postings == 0


async def test_robots_saying_no_is_not_a_verdict_on_the_page() -> None:
    """BLOCKED rather than NO_DATA, so a re-run can tell the two apart — and so
    nothing is retired on the strength of a robots file."""
    result = await probe_page(ROW, _fetcher(_page(JOB), robots="User-agent: *\nDisallow: /"))

    assert result.state is BespokeState.BLOCKED


async def test_a_server_error_is_not_a_verdict_either() -> None:
    result = await probe_page(ROW, _fetcher("nope", status=503))

    assert result.state is BespokeState.UNREACHABLE
    assert result.status == 503


async def test_a_row_with_no_url_never_reaches_the_network() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=ROBOTS_OK)

    fetcher = PoliteFetcher(transport=httpx.MockTransport(handler), rate_limiter=_limiter())
    result = await probe_page(Row(name="Acme", url=""), fetcher)

    assert result.state is BespokeState.UNREACHABLE
    assert calls == []


async def test_one_unreachable_host_does_not_end_the_sweep() -> None:
    """Three thousand rows means some hosts are down at any moment. A sweep
    that stopped at the first would have to be restarted by hand."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_OK)
        if request.url.host == "down.example":
            raise httpx.ConnectError("refused")
        return httpx.Response(200, text=_page(JOB))

    fetcher = PoliteFetcher(transport=httpx.MockTransport(handler), rate_limiter=_limiter())
    rows = [
        Row(name="Down", url="https://down.example/careers"),
        Row(name="Up", url="https://up.example/careers"),
    ]

    report = await probe(rows, fetcher)

    assert [r.state for r in report.results] == [
        BespokeState.UNREACHABLE,
        BespokeState.PUBLISHES,
    ]


async def test_the_limit_stops_early() -> None:
    """3,000 pages is an afternoon. Sampling first is how you find out whether
    the whole sweep is worth running."""
    rows = [Row(name=str(i), url=f"https://h{i}.example/careers") for i in range(5)]

    report = await probe(rows, _fetcher(_page(JOB)), limit=2)

    assert len(report.results) == 2


async def test_a_limit_of_zero_probes_nothing() -> None:
    """Zero is a limit, not the absence of one. A truthiness test read `-n 0`
    as unlimited, so asking for a sample of nothing swept the whole file — on
    ~3,000 rows an afternoon instead of a moment, and every page fetched."""
    rows = [Row(name=str(i), url=f"https://h{i}.example/careers") for i in range(5)]

    report = await probe(rows, _fetcher(_page(JOB)), limit=0)

    assert report.results == []


async def test_no_limit_probes_everything() -> None:
    rows = [Row(name=str(i), url=f"https://h{i}.example/careers") for i in range(5)]

    report = await probe(rows, _fetcher(_page(JOB)))

    assert len(report.results) == 5


async def test_a_page_whose_markup_raises_is_reported_not_fatal() -> None:
    """`probe_page` promises never to raise, and `extract` sat outside its try.
    A page whose JSON-LD carried a shape nothing anticipated would have ended a
    sweep of thousands on its first bad row."""
    hostile = {**JOB, "description": {"@value": "not a string"}, "jobLocation": {"address": []}}

    report = await probe([ROW], _fetcher(_page(hostile)))

    assert len(report.results) == 1
    assert report.results[0].state in (BespokeState.PUBLISHES, BespokeState.NO_DATA)


# --------------------------------------------------------------------------
# Promotion
# --------------------------------------------------------------------------


async def test_only_the_pages_that_answered_become_seeds() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_OK)
        if request.url.host == "bare.example":
            return httpx.Response(200, text="<html><body>Careers</body></html>")
        return httpx.Response(200, text=_page(JOB))

    fetcher = PoliteFetcher(transport=httpx.MockTransport(handler), rate_limiter=_limiter())
    rows = [
        Row(name="Bare", url="https://bare.example/careers"),
        Row(name="Acme", url="https://acme.example/careers"),
    ]

    seeds = to_seeds(await probe(rows, fetcher), [])

    assert [s.name for s in seeds] == ["Acme"]


async def test_a_seed_carries_the_page_url_as_its_slug() -> None:
    """`JsonLdExtractor.board_url` returns the slug unchanged, and `(ats, slug)`
    is the identity key every importer de-duplicates on."""
    seeds = to_seeds(await probe([ROW], _fetcher(_page(JOB))), [])

    assert seeds[0].ats == ATS
    assert seeds[0].slug == ROW.url
    assert seeds[0].careers_url == ROW.url


async def test_a_page_already_in_the_registry_is_not_added_twice() -> None:
    existing = [CompanySeed(name="Acme", slug=ROW.url, ats=ATS)]

    seeds = to_seeds(await probe([ROW], _fetcher(_page(JOB))), existing)

    assert seeds == []


async def test_the_summary_names_every_outcome_that_occurred() -> None:
    report = await probe([ROW], _fetcher(_page(JOB)))

    summary = report.summary()
    assert "1 page probed" in summary
    assert "publishes" in summary
    assert "1 postings" in summary


# --------------------------------------------------------------------------
# Validation, which had one question and now has two
# --------------------------------------------------------------------------


async def test_a_live_bespoke_page_is_not_condemned_by_the_board_check() -> None:
    """`_api_is_board` asks for JSON with a `jobs` list. A careers page returns
    HTML, so without a separate check every live `jsonld` seed would read as
    MISSING and be retired by the first `--write` sweep."""
    from packages.crawler.validate import SeedState, validate_seeds

    seeds = [CompanySeed(name="Acme", slug=ROW.url, ats=ATS, careers_url=ROW.url)]

    results = await validate_seeds(seeds, _fetcher(_page(JOB)))

    assert results[0].state is SeedState.STRUCTURED


async def test_a_bespoke_page_that_stopped_publishing_is_missing() -> None:
    """What makes a `jsonld` seed dead is the page no longer publishing — the
    same thing that makes the crawler stop finding jobs there."""
    from packages.crawler.validate import SeedState, validate_seeds

    seeds = [CompanySeed(name="Acme", slug=ROW.url, ats=ATS, careers_url=ROW.url)]

    results = await validate_seeds(seeds, _fetcher("<html><body>Careers</body></html>"))

    assert results[0].state is SeedState.MISSING
