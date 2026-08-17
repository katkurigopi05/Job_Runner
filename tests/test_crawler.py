"""Crawler — Gate 5, and the §2.6 politeness rules.

§2.6 is a correctness requirement, not a preference: "respects robots.txt and
rate limits. Minimum 60s between requests to the same host. Configurable up,
never down." The tests that matter most here are the ones proving the crawler
*cannot* be configured to misbehave.

Nothing reaches the network. Boards come from recorded fixtures via
httpx.MockTransport, and the rate limiter's clock is injected so the waiting
is proven without waiting.
"""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from packages.crawler.crawl import crawl_all, crawl_company, is_due
from packages.crawler.extract import (
    CompanySeed,
    GreenhouseExtractor,
    load_seed,
    posting_hash,
    strip_html,
)
from packages.crawler.fetch import Blocked, PoliteFetcher, build_fetcher, content_hash
from packages.crawler.ratelimit import (
    MIN_DELAY_SECONDS,
    HostRateLimiter,
    RateLimitTooLow,
)
from packages.crawler.robots import RobotsCache
from packages.crawler.validate import SeedState, validate_seeds


class FakeClock:
    """A clock that only advances when its sleeper is awaited.

    Paired deliberately: a frozen clock with a real sleeper makes `acquire`
    wait forever, which is how the first version of these tests hung.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def limiter(clock: FakeClock | None = None, **kwargs) -> HostRateLimiter:
    fake = clock or FakeClock()
    return HostRateLimiter(clock=fake, sleeper=fake.sleep, **kwargs)


# --------------------------------------------------------------------------
# Rate limiting — the floor is structural
# --------------------------------------------------------------------------


def test_delay_below_the_floor_is_refused() -> None:
    """§2.6 — configurable up, never down. Refused, not clamped."""
    with pytest.raises(RateLimitTooLow, match="configurable upward only"):
        HostRateLimiter(delay_seconds=5.0)


def test_delay_at_or_above_the_floor_is_accepted() -> None:
    assert HostRateLimiter(delay_seconds=MIN_DELAY_SECONDS).delay_seconds == 60.0
    assert HostRateLimiter(delay_seconds=300.0).delay_seconds == 300.0


def test_first_request_to_a_host_is_immediate() -> None:
    lim = limiter()
    assert lim.is_ready("example.com")
    assert lim.time_until_ready("example.com") == 0.0


def test_second_request_must_wait_the_full_delay() -> None:
    clock = FakeClock(1000.0)
    lim = limiter(clock)

    lim.record("example.com")
    assert lim.time_until_ready("example.com") == 60.0

    clock.now += 59.0
    assert lim.time_until_ready("example.com") == pytest.approx(1.0)

    clock.now += 1.0
    assert lim.is_ready("example.com")


def test_hosts_are_tracked_independently() -> None:
    lim = limiter()
    lim.record("a.com")
    assert not lim.is_ready("a.com")
    assert lim.is_ready("b.com")


def test_a_failed_request_still_counts() -> None:
    """A 500 cost the host a round trip; retrying instantly is the abuse."""
    lim = limiter()
    lim.record("example.com")  # caller records regardless of status
    assert not lim.is_ready("example.com")


async def test_acquire_waits_then_records() -> None:
    clock = FakeClock()
    lim = limiter(clock)

    await lim.acquire("example.com")
    waited = await lim.acquire("example.com")

    assert waited == pytest.approx(60.0)
    assert clock.slept == [60.0]


async def test_acquire_fails_loudly_on_a_stopped_clock() -> None:
    """A frozen clock must raise, not spin — that hang cost real debugging."""
    stopped = FakeClock()

    async def never_advance(seconds: float) -> None:
        return None

    lim = HostRateLimiter(clock=stopped, sleeper=never_advance)
    lim.record("example.com")

    with pytest.raises(RuntimeError, match="clock is not advancing"):
        await lim.acquire("example.com")


def test_build_fetcher_never_goes_below_the_floor() -> None:
    """Even a config file asking for 1s produces a compliant limiter."""
    fetcher = build_fetcher(delay_seconds=1.0)
    assert fetcher.rate_limiter is not None
    assert fetcher.rate_limiter.delay_seconds == MIN_DELAY_SECONDS


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------


def _robots_transport(body: str, status: int = 200, fail: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            raise httpx.ConnectError("boom", request=request)
        if request.url.path == "/robots.txt":
            return httpx.Response(status, text=body)
        return httpx.Response(200, text="page")

    return httpx.MockTransport(handler)


async def test_disallowed_path_is_blocked() -> None:
    cache = RobotsCache(transport=_robots_transport("User-agent: *\nDisallow: /jobs"))
    decision = await cache.check("https://example.com/jobs/1")
    assert not decision.allowed
    assert "disallowed" in decision.reason


async def test_allowed_path_passes() -> None:
    cache = RobotsCache(transport=_robots_transport("User-agent: *\nDisallow: /admin"))
    assert (await cache.check("https://example.com/jobs/1")).allowed


async def test_missing_robots_means_unrestricted() -> None:
    """A 404 is a real answer: the standard reads it as no rules."""
    cache = RobotsCache(transport=_robots_transport("", status=404))
    decision = await cache.check("https://example.com/jobs")
    assert decision.allowed
    assert "no robots.txt" in decision.reason


async def test_unreachable_robots_blocks_rather_than_assumes() -> None:
    """If we cannot read the rules, we do not get to assume they favour us."""
    cache = RobotsCache(transport=_robots_transport("", fail=True))
    decision = await cache.check("https://example.com/jobs")
    assert not decision.allowed
    assert "could not be read" in decision.reason


async def test_server_error_on_robots_blocks() -> None:
    cache = RobotsCache(transport=_robots_transport("", status=503))
    assert not (await cache.check("https://example.com/jobs")).allowed


async def test_crawl_delay_is_read() -> None:
    cache = RobotsCache(transport=_robots_transport("User-agent: *\nCrawl-delay: 120\nDisallow:"))
    decision = await cache.check("https://example.com/jobs")
    assert decision.crawl_delay == 120.0


async def test_robots_is_cached_per_host() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text="User-agent: *\nDisallow:")

    cache = RobotsCache(transport=httpx.MockTransport(handler))
    await cache.check("https://example.com/a")
    await cache.check("https://example.com/b")

    assert calls["n"] == 1


# --------------------------------------------------------------------------
# The fetcher enforces both gates
# --------------------------------------------------------------------------


def _board_transport(payload: dict, robots: str = "User-agent: *\nDisallow:"):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        return httpx.Response(200, text=json.dumps(payload))

    return httpx.MockTransport(handler)


def _status_transport(status: int):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        return httpx.Response(status, text="not found")

    return httpx.MockTransport(handler)


def _job(job_id: int, title: str = "Senior Backend Engineer", content: str = "<p>Python</p>"):
    return {
        "id": job_id,
        "title": title,
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "location": {"name": "Remote"},
        "content": content,
    }


async def test_fetch_refuses_a_disallowed_url() -> None:
    fetcher = PoliteFetcher(
        transport=_board_transport({"jobs": []}, robots="User-agent: *\nDisallow: /"),
        rate_limiter=limiter(),
    )
    with pytest.raises(Blocked):
        await fetcher.fetch("https://boards-api.greenhouse.io/v1/boards/acme/jobs")


async def test_fetch_returns_a_content_hash() -> None:
    fetcher = PoliteFetcher(
        transport=_board_transport({"jobs": [_job(1)]}),
        rate_limiter=limiter(),
    )
    result = await fetcher.fetch("https://boards-api.greenhouse.io/v1/boards/acme/jobs")

    assert result.ok
    assert result.content_hash == content_hash(result.text)


async def test_site_crawl_delay_raises_ours_but_never_lowers_it() -> None:
    """A site asking for more space gets it; asking for less changes nothing."""
    slow = PoliteFetcher(
        transport=_board_transport(
            {"jobs": []}, robots="User-agent: *\nCrawl-delay: 300\nDisallow:"
        ),
        rate_limiter=limiter(),
    )
    await slow.fetch("https://example.com/board")
    assert slow.rate_limiter is not None
    assert slow.rate_limiter.delay_seconds == 300.0

    fast = PoliteFetcher(
        transport=_board_transport({"jobs": []}, robots="User-agent: *\nCrawl-delay: 1\nDisallow:"),
        rate_limiter=limiter(),
    )
    await fast.fetch("https://example.com/board")
    assert fast.rate_limiter is not None
    assert fast.rate_limiter.delay_seconds == MIN_DELAY_SECONDS


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


def test_greenhouse_board_url() -> None:
    assert "acme" in GreenhouseExtractor().board_url("acme")


def test_greenhouse_parses_a_board() -> None:
    body = json.dumps({"jobs": [_job(1), _job(2, title="Data Engineer")]})
    postings = GreenhouseExtractor().parse(body, "acme")

    assert len(postings) == 2
    assert postings[0].external_id == "1"
    assert postings[0].title == "Senior Backend Engineer"
    assert postings[0].location == "Remote"
    assert postings[0].ats_type == "greenhouse"


def test_greenhouse_survives_malformed_json() -> None:
    assert GreenhouseExtractor().parse("not json", "acme") == []


def test_missing_fields_stay_none_rather_than_guessed() -> None:
    body = json.dumps({"jobs": [{"id": 7, "absolute_url": "https://x/7"}]})
    posting = GreenhouseExtractor().parse(body, "acme")[0]
    assert posting.title is None
    assert posting.location is None
    assert posting.description_raw is None


def test_strip_html() -> None:
    assert strip_html("<p>Hello <b>world</b></p>") == "Hello world"
    assert strip_html("<p>a</p><p>b</p>") == "a\nb"
    assert strip_html("&amp;") == "&"
    assert strip_html(None) is None
    assert strip_html("<div></div>") is None


def test_script_and_style_are_not_description_text() -> None:
    """Code inside a posting is not something a human wrote about the job.

    description_raw feeds embeddings and the fabrication guard, so a tracking
    snippet landing in it is treated downstream as a fact from the source.
    """
    assert strip_html("<p>Real text.</p><script>var trackingId=42;</script>") == "Real text."
    assert strip_html("<style>.a{color:red}</style><p>Real text.</p>") == "Real text."
    assert strip_html("<noscript>Enable JS</noscript><p>Real.</p>") == "Real."


def test_unclosed_block_still_separates_words() -> None:
    """Boards do emit unclosed <p>. Welding two paragraphs invents a word."""
    assert (
        strip_html("<div><p>Unclosed <strong>bold<p>Next para</div>") == "Unclosed bold\nNext para"
    )


def test_inline_tags_do_not_split_words() -> None:
    assert strip_html("<p>We use <strong>Python</strong> and <em>Go</em>.</p>") == (
        "We use Python and Go."
    )


def test_posting_hash_changes_with_content() -> None:
    a = GreenhouseExtractor().parse(json.dumps({"jobs": [_job(1)]}), "acme")[0]
    b = GreenhouseExtractor().parse(json.dumps({"jobs": [_job(1, content="<p>Rust</p>")]}), "acme")[
        0
    ]
    assert posting_hash(a) != posting_hash(b)


def test_seed_registry_loads() -> None:
    seeds = load_seed()
    assert len(seeds) >= 40, "the registry should be hand-picked, not empty"
    assert all(s.slug for s in seeds)
    assert all(s.ats == "greenhouse" for s in seeds)


async def test_seed_validation_checks_rendered_board_after_api_404() -> None:
    statuses = {
        ("boards-api.greenhouse.io", "api-only"): 200,
        ("boards-api.greenhouse.io", "rendered-only"): 404,
        ("job-boards.greenhouse.io", "rendered-only"): 200,
        ("boards-api.greenhouse.io", "missing"): 404,
        ("job-boards.greenhouse.io", "missing"): 404,
    }
    request_times: dict[str, list[float]] = {}
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        request_times.setdefault(request.url.host, []).append(clock.now)
        if request.url.host.startswith("boards-api"):
            slug = request.url.path.split("/")[-2]
            body = json.dumps({"jobs": []})
        else:
            slug = request.url.path.strip("/")
            body = "<title>Jobs</title>"
        return httpx.Response(statuses[(request.url.host, slug)], text=body)

    fetcher = PoliteFetcher(transport=httpx.MockTransport(handler), rate_limiter=limiter(clock))
    seeds = [
        CompanySeed(name="API", slug="api-only"),
        CompanySeed(name="Rendered", slug="rendered-only"),
        CompanySeed(name="Missing", slug="missing"),
    ]

    results = await validate_seeds(seeds, fetcher)

    assert [result.state for result in results] == [
        SeedState.API,
        SeedState.RENDERED_ONLY,
        SeedState.MISSING,
    ]
    assert request_times["boards-api.greenhouse.io"] == [0.0, 60.0, 120.0]
    rendered_times = request_times["job-boards.greenhouse.io"]
    assert rendered_times[1] - rendered_times[0] >= MIN_DELAY_SECONDS


async def test_seed_validation_keeps_recorded_non_greenhouse_ats() -> None:
    fetcher = PoliteFetcher(transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    results = await validate_seeds([CompanySeed(name="Moved", slug="moved", ats="lever")], fetcher)

    assert results[0].state is SeedState.OTHER_ATS


# --------------------------------------------------------------------------
# Gate 5 — change detection
# --------------------------------------------------------------------------


@pytest.fixture
def seed() -> CompanySeed:
    return CompanySeed(name="Acme", slug="acme", poll_interval_s=0)


async def test_first_crawl_emits_postings(db_session, seed) -> None:
    fetcher = PoliteFetcher(
        transport=_board_transport({"jobs": [_job(1), _job(2)]}),
        rate_limiter=limiter(),
    )

    result = await crawl_company(db_session, seed, fetcher, force=True)

    assert result.fetched
    assert result.new_postings == 2
    assert result.emitted == 2


async def test_second_crawl_emits_nothing(db_session, seed) -> None:
    """Gate 5: a second run over unchanged boards emits zero postings."""
    payload = {"jobs": [_job(1), _job(2)]}
    fetcher = PoliteFetcher(
        transport=_board_transport(payload),
        rate_limiter=limiter(),
    )

    first = await crawl_company(db_session, seed, fetcher, force=True)
    second = await crawl_company(db_session, seed, fetcher, force=True)

    assert first.emitted == 2
    assert second.emitted == 0


async def test_an_edited_posting_is_emitted_again(db_session, seed) -> None:
    fetcher_a = PoliteFetcher(
        transport=_board_transport({"jobs": [_job(1)]}),
        rate_limiter=limiter(),
    )
    await crawl_company(db_session, seed, fetcher_a, force=True)

    fetcher_b = PoliteFetcher(
        transport=_board_transport({"jobs": [_job(1, content="<p>Now with Rust</p>")]}),
        rate_limiter=limiter(),
    )
    second = await crawl_company(db_session, seed, fetcher_b, force=True)

    assert second.updated_postings == 1


async def test_a_removed_posting_is_closed(db_session, seed) -> None:
    fetcher_a = PoliteFetcher(
        transport=_board_transport({"jobs": [_job(1), _job(2)]}),
        rate_limiter=limiter(),
    )
    await crawl_company(db_session, seed, fetcher_a, force=True)

    fetcher_b = PoliteFetcher(
        transport=_board_transport({"jobs": [_job(1)]}),
        rate_limiter=limiter(),
    )
    second = await crawl_company(db_session, seed, fetcher_b, force=True)

    assert second.closed_postings == 1


async def test_poll_interval_is_respected(db_session) -> None:
    seed = CompanySeed(name="Acme", slug="acme", poll_interval_s=86400)
    fetcher = PoliteFetcher(
        transport=_board_transport({"jobs": [_job(1)]}),
        rate_limiter=limiter(),
    )

    await crawl_company(db_session, seed, fetcher, force=True)
    second = await crawl_company(db_session, seed, fetcher)

    assert second.skipped_reason == "not due yet"


async def test_blocked_company_is_skipped_not_fatal(db_session, seed) -> None:
    """Being told no is a normal outcome; the cycle carries on."""
    fetcher = PoliteFetcher(
        transport=_board_transport({"jobs": []}, robots="User-agent: *\nDisallow: /"),
        rate_limiter=limiter(),
    )

    report = await crawl_all(db_session, [seed], fetcher, force=True)

    assert report.emitted == 0
    assert report.blocked == ["Acme"]


async def test_404_board_is_reported_not_counted_as_empty(db_session, seed) -> None:
    """A dead API board must be visible in worker output, not look healthy."""
    fetcher = PoliteFetcher(transport=_status_transport(404), rate_limiter=limiter())

    report = await crawl_all(db_session, [seed], fetcher, force=True)

    assert report.fetched == 1
    assert report.failed == ["Acme"]
    assert report.results[0].error == "HTTP 404"
    assert "Acme: HTTP 404" in report.summary()


async def test_empty_200_board_is_not_a_failure(db_session, seed) -> None:
    fetcher = PoliteFetcher(transport=_board_transport({"jobs": []}), rate_limiter=limiter())

    report = await crawl_all(db_session, [seed], fetcher, force=True)

    assert report.emitted == 0
    assert report.failed == []


async def test_unknown_ats_is_skipped(db_session) -> None:
    seed = CompanySeed(name="Acme", slug="acme", ats="workday")
    fetcher = PoliteFetcher(
        transport=_board_transport({"jobs": []}),
        rate_limiter=limiter(),
    )

    result = await crawl_company(db_session, seed, fetcher, force=True)

    assert result.skipped_reason is not None
    assert "workday" in result.skipped_reason


async def test_full_cycle_respects_the_rate_limit(db_session) -> None:
    """Gate 5: a cycle over the registry never exceeds the per-host limit."""
    clock = FakeClock()
    seeds = [CompanySeed(name=f"C{i}", slug=f"c{i}", poll_interval_s=0) for i in range(5)]
    fetcher = PoliteFetcher(
        transport=_board_transport({"jobs": [_job(1)]}), rate_limiter=limiter(clock)
    )

    report = await crawl_all(db_session, seeds, fetcher, force=True)

    assert report.fetched == 5
    # Same host every time, so four full-delay waits between five fetches.
    assert clock.slept == [60.0] * 4
    assert all(s >= MIN_DELAY_SECONDS for s in clock.slept)


def test_is_due_without_a_previous_poll(db_session) -> None:
    from packages.core.models import Company

    assert is_due(Company(name="Never polled", poll_interval_s=3600))


# --------------------------------------------------------------------------
# The worker seam — handle_crawl wired to the real cycle
# --------------------------------------------------------------------------


async def test_crawl_handler_runs_a_cycle_and_scores(
    worker_session, committing_sessionmaker, monkeypatch, tmp_path
) -> None:
    """handle_crawl → crawl_all → embed → score, with only the socket faked.

    This closes the same seam that bit the apply pipeline: unit tests of the
    crawler would not notice handle_crawl drifting away from it.
    """
    from apps.worker import crawl_job
    from packages.core.models import Candidate as CandidateModel
    from packages.core.models import Match, Posting, Profile, User
    from packages.core.queue import ClaimedTask, enqueue

    seed_file = tmp_path / "companies.yaml"
    seed_file.write_text(
        'companies:\n  - name: "Acme"\n    slug: "acme"\n'
        '    ats: "greenhouse"\n    poll_interval_s: 0\n'
    )

    clock = FakeClock()
    fetcher = PoliteFetcher(
        transport=_board_transport(
            {"jobs": [_job(1, title="Senior Backend Engineer", content="<p>Python</p>")]}
        ),
        rate_limiter=limiter(clock),
    )
    monkeypatch.setattr(crawl_job, "build_fetcher", lambda *a, **k: fetcher)

    user = User(email="crawl-owner@example.com")
    worker_session.add(user)
    await worker_session.flush()
    candidate = CandidateModel(user_id=user.id, name="Owner", email="crawl@example.com")
    worker_session.add(candidate)
    await worker_session.flush()
    worker_session.add(Profile(candidate_id=candidate.id, label="backend"))
    task = await enqueue(worker_session, "crawl", {"seed_path": str(seed_file), "force": True})
    await worker_session.commit()

    await crawl_job.handle_crawl(
        worker_session, ClaimedTask(task=task, reclaimed=False, previous_owner=None)
    )
    await worker_session.commit()

    postings = list((await worker_session.scalars(select(Posting))).all())
    assert len(postings) == 1
    assert postings[0].description_embedding is not None, "postings must be embedded"

    matches = list((await worker_session.scalars(select(Match))).all())
    assert len(matches) == 1
    assert matches[0].reasons_json is not None
