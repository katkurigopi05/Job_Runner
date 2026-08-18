"""Discovery — aggregators, ATS resolution, and registry growth.

The registry answers "what is new at the companies the owner chose". These
answer the other question, the one a curated list structurally cannot: what
is open at a company nobody thought to list.
"""

from __future__ import annotations

import json

import httpx

from packages.crawler.aggregators import (
    ArbeitnowSource,
    RemoteOkSource,
    RemotiveSource,
    fetch_source,
)
from packages.crawler.discover import ingest, promote, slug_from_ats_url
from packages.crawler.fetch import PoliteFetcher
from packages.crawler.ratelimit import HostRateLimiter
from packages.crawler.resolve import find_embedded, resolve, resolve_from_url


class FakeClock:
    """A clock that only advances when its sleeper is awaited.

    Paired deliberately. A frozen clock with the real sleeper makes `acquire`
    wait for a delay that can never elapse, which is exactly how the first
    version of this file hung.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def limiter() -> HostRateLimiter:
    clock = FakeClock()
    return HostRateLimiter(clock=clock, sleeper=clock.sleep)


def transport(routes: dict[str, httpx.Response]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        for fragment, response in routes.items():
            if fragment in str(request.url):
                return response
        return httpx.Response(404, text="")

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


def test_remotive_parses_its_feed() -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 42,
                    "title": "Backend Engineer",
                    "url": "https://remotive.com/j/42",
                    "company_name": "Acme",
                    "candidate_required_location": "Worldwide",
                    "description": "<p>Python and Postgres.</p>",
                }
            ]
        }
    )

    postings = RemotiveSource().parse(body)

    assert len(postings) == 1
    assert postings[0].company_name == "Acme"
    assert "Python" in (postings[0].posting.description_raw or "")
    # A lead until resolution proves otherwise.
    assert postings[0].posting.ats_type == "unknown"


def test_external_ids_are_namespaced_by_source() -> None:
    """Two aggregators both numbering from 1 must not collide."""
    remotive = RemotiveSource().parse(json.dumps({"jobs": [{"id": 1, "title": "A"}]}))
    arbeitnow = ArbeitnowSource().parse(json.dumps({"data": [{"slug": "1", "title": "A"}]}))

    assert remotive[0].posting.external_id != arbeitnow[0].posting.external_id


def test_remoteok_skips_the_legal_notice() -> None:
    """The first element of that feed is a disclaimer, not a job."""
    body = json.dumps(
        [
            {"legal": "See remoteok.com/terms"},
            {"id": "7", "position": "SRE", "company": "Globex"},
        ]
    )

    postings = RemoteOkSource().parse(body)

    assert len(postings) == 1
    assert postings[0].posting.title == "SRE"


async def test_a_source_that_returns_nothing_says_why() -> None:
    """An empty list and a broken source look identical without this."""
    fetcher = PoliteFetcher(
        transport=transport({"remotive": httpx.Response(200, text=json.dumps({"jobs": []}))}),
        rate_limiter=limiter(),
    )

    result = await fetch_source(RemotiveSource(), fetcher)

    assert not result.ok
    assert result.failure is not None


async def test_an_unreadable_200_is_not_an_empty_board() -> None:
    fetcher = PoliteFetcher(
        transport=transport({"remotive": httpx.Response(200, text="<html>nope</html>")}),
        rate_limiter=limiter(),
    )

    result = await fetch_source(RemotiveSource(), fetcher)

    assert not result.ok
    assert "unreadable" in (result.failure or "")


async def test_a_rate_limited_source_names_the_reason() -> None:
    fetcher = PoliteFetcher(
        transport=transport({"remotive": httpx.Response(429, text="slow down")}),
        rate_limiter=limiter(),
    )

    result = await fetch_source(RemotiveSource(), fetcher)

    assert "rate limited" in (result.failure or "")


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def test_a_supported_url_resolves_without_a_fetch() -> None:
    resolution = resolve_from_url("https://boards.greenhouse.io/acme/jobs/12345")

    assert resolution.ats == "greenhouse"
    assert resolution.via == "url"


def test_an_aggregator_url_does_not_resolve_on_shape_alone() -> None:
    assert not resolve_from_url("https://remotive.com/j/42").applyable


def test_an_embedded_form_is_found_in_a_careers_page() -> None:
    html = '<a href="https://jobs.lever.co/globex/abc123def4567890">Apply</a>'

    assert find_embedded(html) == "https://jobs.lever.co/globex/abc123def4567890"


def test_prose_mentioning_an_ats_is_not_a_form() -> None:
    """Resolution must never guess. A wrong ats sends the worker to fill a
    form that is not there, and it surfaces as site_error on a real
    application rather than as an honest manual completion."""
    assert find_embedded("<p>We use Greenhouse for hiring.</p>") is None


async def test_resolution_follows_the_posting_page() -> None:
    fetcher = PoliteFetcher(
        transport=transport(
            {
                "remotive.com/j/42": httpx.Response(
                    200,
                    text='<a href="https://boards.greenhouse.io/acme/jobs/99">Apply</a>',
                )
            }
        ),
        rate_limiter=limiter(),
    )

    resolution = await resolve("https://remotive.com/j/42", fetcher)

    assert resolution.ats == "greenhouse"
    assert resolution.via == "posting"
    assert resolution.url == "https://boards.greenhouse.io/acme/jobs/99"


async def test_an_unresolvable_posting_is_still_a_lead() -> None:
    fetcher = PoliteFetcher(
        transport=transport({"remotive.com": httpx.Response(200, text="<p>Apply by email</p>")}),
        rate_limiter=limiter(),
    )

    resolution = await resolve("https://remotive.com/j/42", fetcher)

    assert not resolution.applyable
    assert resolution.url == "https://remotive.com/j/42"


# --------------------------------------------------------------------------
# Ingest and registry growth
# --------------------------------------------------------------------------


def test_slug_is_read_from_every_supported_board() -> None:
    assert slug_from_ats_url("https://boards.greenhouse.io/acme/jobs/1") == "acme"
    assert slug_from_ats_url("https://jobs.lever.co/globex/abc123def4567890") == "globex"
    assert slug_from_ats_url("https://jobs.ashbyhq.com/initech/abc123def4567890") == "initech"
    assert slug_from_ats_url("https://remotive.com/j/42") is None


def _feed() -> httpx.Response:
    return httpx.Response(
        200,
        text=json.dumps(
            {
                "jobs": [
                    {
                        "id": 42,
                        "title": "Backend Engineer",
                        "url": "https://remotive.com/j/42",
                        "company_name": "Acme",
                        "description": "Python",
                    }
                ]
            }
        ),
    )


async def test_ingest_stores_a_posting_from_a_company_nobody_listed(db_session) -> None:
    fetcher = PoliteFetcher(
        transport=transport(
            {
                "remotive.com/api": _feed(),
                "remotive.com/j/42": httpx.Response(
                    200, text='<a href="https://boards.greenhouse.io/acme/jobs/99">Apply</a>'
                ),
            }
        ),
        rate_limiter=limiter(),
    )

    report = await ingest(db_session, fetcher, sources=(RemotiveSource(),))

    assert report.new_postings == 1
    assert report.resolved == 1


async def test_ingest_is_idempotent(db_session) -> None:
    """Aggregator feeds repeat; a second pass must not duplicate."""
    fetcher = PoliteFetcher(
        transport=transport({"remotive.com/api": _feed()}),
        rate_limiter=limiter(),
    )

    first = await ingest(db_session, fetcher, sources=(RemotiveSource(),), resolve_ats=False)
    second = await ingest(db_session, fetcher, sources=(RemotiveSource(),), resolve_ats=False)

    assert first.new_postings == 1
    assert second.new_postings == 0


async def test_a_resolved_company_is_promoted_into_the_registry(db_session, tmp_path) -> None:
    """The part that compounds: a resolved posting makes its company
    pollable first-hand from then on."""
    seed_file = tmp_path / "companies.yaml"
    seed_file.write_text("companies:\n  - name: Stripe\n    slug: stripe\n    ats: greenhouse\n")

    fetcher = PoliteFetcher(
        transport=transport(
            {
                "remotive.com/api": _feed(),
                "remotive.com/j/42": httpx.Response(
                    200, text='<a href="https://boards.greenhouse.io/acme/jobs/99">Apply</a>'
                ),
            }
        ),
        rate_limiter=limiter(),
    )
    await ingest(db_session, fetcher, sources=(RemotiveSource(),))

    added = await promote(db_session, seed_path=str(seed_file))

    assert [seed.slug for seed in added] == ["acme"]
    assert "acme" in seed_file.read_text()
    # The owner's own entry is untouched.
    assert "stripe" in seed_file.read_text()


async def test_promotion_does_not_duplicate_a_known_company(db_session, tmp_path) -> None:
    seed_file = tmp_path / "companies.yaml"
    seed_file.write_text("companies:\n  - name: Acme\n    slug: acme\n    ats: greenhouse\n")

    fetcher = PoliteFetcher(
        transport=transport(
            {
                "remotive.com/api": _feed(),
                "remotive.com/j/42": httpx.Response(
                    200, text='<a href="https://boards.greenhouse.io/acme/jobs/99">Apply</a>'
                ),
            }
        ),
        rate_limiter=limiter(),
    )
    await ingest(db_session, fetcher, sources=(RemotiveSource(),))

    assert await promote(db_session, seed_path=str(seed_file)) == []


# --------------------------------------------------------------------------
# The worker pool
# --------------------------------------------------------------------------


async def test_pool_members_get_distinct_worker_ids(monkeypatch) -> None:
    """The id is what lets a restarted worker recognize its own expired
    lease. Sharing one across a pool would let a member resume a task
    another member is still running."""
    from apps.worker import run as worker_run

    seen: list[str] = []

    async def fake_run_forever(*, worker_id=None, lease_seconds=None):
        seen.append(worker_id)

    monkeypatch.setattr(worker_run, "run_forever", fake_run_forever)
    await worker_run.run_pool(3)

    assert len(set(seen)) == 3


def test_discovery_is_a_registered_task_kind() -> None:
    from apps.worker.discover_job import DISCOVER_TASK_KIND
    from apps.worker.run import HANDLERS

    assert DISCOVER_TASK_KIND in HANDLERS
