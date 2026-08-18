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
    AshbyExtractor,
    CompanySeed,
    GreenhouseExtractor,
    LeverExtractor,
    load_seed,
    posting_hash,
    strip_html,
)
from packages.crawler.fetch import Blocked, PoliteFetcher, build_fetcher, content_hash
from packages.crawler.ratelimit import (
    MIN_DELAY_SECONDS,
    MIN_SHARED_API_DELAY_SECONDS,
    HostRateLimiter,
    RateLimitTooLow,
    floor_for,
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
    # Recorded against the host that asked, not globally.
    assert slow.rate_limiter.delay_for("example.com") == 300.0
    assert slow.rate_limiter.delay_seconds == MIN_DELAY_SECONDS

    fast = PoliteFetcher(
        transport=_board_transport({"jobs": []}, robots="User-agent: *\nCrawl-delay: 1\nDisallow:"),
        rate_limiter=limiter(),
    )
    await fast.fetch("https://example.com/board")
    assert fast.rate_limiter is not None
    assert fast.rate_limiter.delay_for("example.com") == MIN_DELAY_SECONDS


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
    """The registry is hand-picked, and every entry is one this project can use.

    This asserted `len(seeds) >= 40` and passed while 21 of the 50 entries were
    dead boards. Size is not health: a 404 board yields zero postings, which
    reads identically to "nothing new since the last poll", so the count stayed
    reassuring while discovery covered barely half the list.

    Liveness needs the network and belongs in `make validate-seeds`. What can
    be checked offline is that entries are well-formed, unique, and for an ATS
    there is an adapter for.
    """
    seeds = load_seed()

    assert len(seeds) >= 20, "the registry should be hand-picked, not empty"
    assert all(s.slug for s in seeds)
    assert all(s.ats == "greenhouse" for s in seeds), "no adapter for other ATSes yet"

    slugs = [s.slug for s in seeds]
    assert len(slugs) == len(set(slugs)), "a duplicate slug polls the same board twice"


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
    # The API host is the shared multi-tenant one; the rendered board is
    # not, and still gets the full §2.6 floor. Both are asserted here so a
    # future edit cannot quietly promote the second one.
    api_gap = MIN_SHARED_API_DELAY_SECONDS
    assert request_times["boards-api.greenhouse.io"] == [0.0, api_gap, api_gap * 2]
    rendered_times = request_times["job-boards.greenhouse.io"]
    assert rendered_times[1] - rendered_times[0] >= MIN_DELAY_SECONDS


async def test_seed_validation_keeps_recorded_unsupported_ats() -> None:
    """An ATS we cannot read is reported, never rewritten or dropped."""
    fetcher = PoliteFetcher(transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    results = await validate_seeds(
        [CompanySeed(name="Moved", slug="moved", ats="workday")], fetcher
    )

    assert results[0].state is SeedState.OTHER_ATS


async def test_seed_validation_survives_a_blocked_host() -> None:
    """One unreadable robots.txt must not abandon the rest of the list."""
    fetcher = PoliteFetcher(transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    results = await validate_seeds(
        [CompanySeed(name="Blocked", slug="blocked", ats="lever")], fetcher
    )

    assert results[0].state is SeedState.BLOCKED


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


async def test_an_empty_board_does_not_close_everything(db_session, seed) -> None:
    """A parse that yields nothing is treated as a broken extractor.

    HTTP 200 with a payload we cannot read is indistinguishable from an
    employer closing every requisition at once, and the two call for opposite
    responses. Guessing "they closed" drops the postings out of the match feed
    with no error raised anywhere, so the crawler declines to guess.
    """
    fetcher_a = PoliteFetcher(
        transport=_board_transport({"jobs": [_job(1), _job(2)]}),
        rate_limiter=limiter(),
    )
    await crawl_company(db_session, seed, fetcher_a, force=True)

    # The shape changed; the extractor now reads nothing out of a 200.
    fetcher_b = PoliteFetcher(
        transport=_board_transport({"results": [{"id": 1}]}),
        rate_limiter=limiter(),
    )
    second = await crawl_company(db_session, seed, fetcher_b, force=True)

    assert second.closed_postings == 0
    assert second.suspect_parse


async def test_a_suspect_parse_is_retried_rather_than_cached(db_session, seed) -> None:
    """The board hash is not recorded, so the next cycle re-reads and re-warns."""
    await crawl_company(
        db_session,
        seed,
        PoliteFetcher(transport=_board_transport({"jobs": [_job(1)]}), rate_limiter=limiter()),
        force=True,
    )
    broken = PoliteFetcher(transport=_board_transport({"results": []}), rate_limiter=limiter())

    first = await crawl_company(db_session, seed, broken, force=True)
    again = await crawl_company(db_session, seed, broken, force=True)

    assert first.suspect_parse
    # Had the hash been recorded, this second pass would short-circuit on
    # "unchanged" and report nothing at all.
    assert again.suspect_parse


async def test_a_genuinely_empty_board_with_nothing_open_is_not_suspect(db_session, seed) -> None:
    """No open postings means nothing to protect, and nothing to warn about."""
    fetcher = PoliteFetcher(transport=_board_transport({"jobs": []}), rate_limiter=limiter())

    result = await crawl_company(db_session, seed, fetcher, force=True)

    assert not result.suspect_parse
    assert result.closed_postings == 0


async def test_suspect_boards_are_named_in_the_report(db_session, seed) -> None:
    await crawl_company(
        db_session,
        seed,
        PoliteFetcher(transport=_board_transport({"jobs": [_job(1)]}), rate_limiter=limiter()),
        force=True,
    )
    report = await crawl_all(
        db_session,
        [seed],
        PoliteFetcher(transport=_board_transport({"results": []}), rate_limiter=limiter()),
        force=True,
    )

    assert report.suspect == ["Acme"]
    assert "suspect" in report.summary()


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
    # Same host every time, so four waits between five fetches. That host is
    # the shared Greenhouse API, which has its own floor — the cycle is fast
    # because the floor is lower, never because a floor was skipped.
    floor = floor_for("boards-api.greenhouse.io")
    assert floor == MIN_SHARED_API_DELAY_SECONDS
    assert clock.slept == [floor] * 4
    assert all(s >= floor for s in clock.slept)


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


# --------------------------------------------------------------------------
# Lever and Ashby — the two ATSes we could already apply to but not find
# --------------------------------------------------------------------------


def test_lever_board_url_carries_the_slug() -> None:
    assert "acme" in LeverExtractor().board_url("acme")


def test_lever_parses_a_board() -> None:
    body = json.dumps(
        [
            {
                "id": "abc-123",
                "text": "Senior Backend Engineer",
                "hostedUrl": "https://jobs.lever.co/acme/abc-123",
                "categories": {"location": "Remote - US"},
                "description": "<p>Build services.</p>",
                "lists": [{"text": "Requirements", "content": "<li>Python</li>"}],
                "additional": "<p>We offer equity.</p>",
            }
        ]
    )

    postings = LeverExtractor().parse(body, "acme")

    assert len(postings) == 1
    assert postings[0].external_id == "abc-123"
    assert postings[0].title == "Senior Backend Engineer"
    assert postings[0].location == "Remote - US"
    assert postings[0].ats_type == "lever"


def test_lever_keeps_the_requirements_not_just_the_blurb() -> None:
    """`description` is the opening pitch; the requirements live in `lists`.

    Reading only `description` would hand the matcher a posting with none of
    the skills in it, which scores every role the same.
    """
    body = json.dumps(
        [
            {
                "id": "1",
                "text": "Engineer",
                "description": "<p>Join us.</p>",
                "lists": [{"text": "You have", "content": "<li>Kubernetes</li>"}],
                "additional": "<p>Bonus: Rust.</p>",
            }
        ]
    )

    described = LeverExtractor().parse(body, "acme")[0].description_raw or ""

    assert "Kubernetes" in described
    assert "Rust" in described


def test_lever_rejects_a_non_list_payload() -> None:
    """Lever returns a bare array; an object means the API changed."""
    assert LeverExtractor().parse(json.dumps({"jobs": []}), "acme") == []
    assert LeverExtractor().parse("not json", "acme") == []


def test_ashby_parses_a_board() -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": "9f8e",
                    "title": "Data Engineer",
                    "location": "Berlin",
                    "jobUrl": "https://jobs.ashbyhq.com/acme/9f8e",
                    "descriptionHtml": "<p>Pipelines in Python.</p>",
                }
            ]
        }
    )

    postings = AshbyExtractor().parse(body, "acme")

    assert len(postings) == 1
    assert postings[0].title == "Data Engineer"
    assert postings[0].location == "Berlin"
    assert postings[0].ats_type == "ashby"
    assert "Python" in (postings[0].description_raw or "")


def test_ashby_falls_back_to_plain_description() -> None:
    """A board that sends only plain text is not an empty posting."""
    body = json.dumps(
        {"jobs": [{"id": "1", "title": "Engineer", "descriptionPlain": "Rust and Go."}]}
    )

    posting = AshbyExtractor().parse(body, "acme")[0]

    assert "Rust" in (posting.description_raw or "")


def test_ashby_rejects_a_non_object_payload() -> None:
    assert AshbyExtractor().parse(json.dumps([]), "acme") == []
    assert AshbyExtractor().parse("not json", "acme") == []


def test_every_ats_we_can_apply_to_can_also_be_crawled() -> None:
    """The gap this closed: adapters existed for three, extractors for one.

    A posting we cannot discover is a posting we never get to apply to, so
    the two registries have to stay in step.
    """
    from packages.ats.registry import ADAPTERS
    from packages.crawler.extract import EXTRACTORS

    assert {adapter.name for adapter in ADAPTERS} <= set(EXTRACTORS)


# --------------------------------------------------------------------------
# §2.6 — the shared-ATS-host floor, and what keeps it honest
# --------------------------------------------------------------------------


def test_a_company_host_still_gets_the_full_floor() -> None:
    limiter_ = HostRateLimiter()

    assert limiter_.delay_for("careers.acme.com") == MIN_DELAY_SECONDS
    assert floor_for("careers.acme.com") == MIN_DELAY_SECONDS


def test_a_shared_ats_api_gets_the_shared_floor() -> None:
    """One endpoint serves every Greenhouse board, so keying on host alone
    serializes the whole registry behind one counter."""
    limiter_ = HostRateLimiter()

    for host in ("boards-api.greenhouse.io", "api.lever.co", "api.ashbyhq.com"):
        assert limiter_.delay_for(host) == MIN_SHARED_API_DELAY_SECONDS


def test_an_unknown_host_is_never_promoted_to_shared() -> None:
    """The list is explicit; nothing gets the faster floor by resembling it."""
    limiter_ = HostRateLimiter()

    assert limiter_.delay_for("boards-api.greenhouse.io.evil.test") == MIN_DELAY_SECONDS
    assert limiter_.delay_for("api.lever.co.attacker.test") == MIN_DELAY_SECONDS


def test_a_shared_host_override_below_its_floor_is_refused() -> None:
    """The shared floor is a floor too — refused, never clamped."""
    with pytest.raises(RateLimitTooLow):
        HostRateLimiter(host_delays={"api.lever.co": 0.1})


def test_a_company_host_override_below_60s_is_refused() -> None:
    with pytest.raises(RateLimitTooLow):
        HostRateLimiter(host_delays={"careers.acme.com": 5.0})


def test_a_429_backs_the_host_off() -> None:
    """The half that makes a faster floor defensible: we listen."""
    now = 1000.0
    limiter_ = HostRateLimiter(clock=lambda: now)

    limiter_.penalize("api.lever.co", 300.0)

    assert limiter_.time_until_ready("api.lever.co") == 300.0
    assert limiter_.is_ready("boards-api.greenhouse.io")


def test_a_penalty_only_ever_extends() -> None:
    """A server asking for a shorter pause does not shorten ours."""
    now = 1000.0
    limiter_ = HostRateLimiter(clock=lambda: now)

    limiter_.penalize("api.lever.co", 300.0)
    limiter_.penalize("api.lever.co", 5.0)

    assert limiter_.time_until_ready("api.lever.co") == 300.0


async def test_retry_after_is_honoured_from_the_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        return httpx.Response(429, headers={"Retry-After": "120"}, text="slow down")

    limiter_ = HostRateLimiter(host_delays={"api.lever.co": 2.0})
    fetcher = PoliteFetcher(transport=httpx.MockTransport(handler), rate_limiter=limiter_)

    result = await fetcher.fetch("https://api.lever.co/v0/postings/acme?mode=json")

    assert result.status == 429
    assert limiter_.time_until_ready("api.lever.co") >= 119


async def test_one_sites_crawl_delay_does_not_slow_every_other_host() -> None:
    """A crawl-delay is that site's request, not a global setting."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 600\nDisallow:")
        return httpx.Response(200, text="{}")

    limiter_ = HostRateLimiter(clock=lambda: 0.0, sleeper=_never_sleep)
    fetcher = PoliteFetcher(transport=httpx.MockTransport(handler), rate_limiter=limiter_)

    await fetcher.fetch("https://careers.slowsite.test/jobs")

    assert limiter_.delay_for("careers.slowsite.test") == 600.0
    assert limiter_.delay_for("careers.other.test") == MIN_DELAY_SECONDS
    assert limiter_.delay_seconds == MIN_DELAY_SECONDS


async def _never_sleep(seconds: float) -> None:
    raise AssertionError(f"should not have waited {seconds}s on a first request")


# --------------------------------------------------------------------------
# Workable — §8's fourth ATS
# --------------------------------------------------------------------------

#: Trimmed from apply.workable.com/api/v1/widget/accounts/open-252?details=true
_WORKABLE_BOARD = """
{"name": "Open", "description": "<p>An AI platform.</p>", "jobs": [
  {"shortcode": "2F30FC3103", "title": "CSM (Customer Success Manager)",
   "city": "Amsterdam", "state": "North Holland", "country": "Netherlands",
   "url": "https://apply.workable.com/j/2F30FC3103",
   "application_url": "https://apply.workable.com/j/2F30FC3103/apply",
   "published_on": "2026-06-14", "created_at": "2026-06-14",
   "description": "<p>Support enterprise customers.</p><script>t(1)</script>"}
]}
"""


def test_workable_board_parses() -> None:
    from packages.crawler.extract import WorkableExtractor

    postings = WorkableExtractor().parse(_WORKABLE_BOARD, "open-252")

    assert len(postings) == 1
    assert postings[0].external_id == "2F30FC3103"
    assert postings[0].title == "CSM (Customer Success Manager)"


def test_workable_url_is_rebuilt_with_the_company_segment() -> None:
    """The API returns `apply.workable.com/j/<code>` — no company.

    The adapter's pattern requires one, so a posting stored with the bare form
    would not route to an adapter when it came time to apply. That failure is
    silent: the posting looks fine in the feed and fails as `unsupported_site`
    at the moment someone tries to use it.
    """
    from packages.ats.workable import WorkableAdapter
    from packages.crawler.extract import WorkableExtractor

    posting = WorkableExtractor().parse(_WORKABLE_BOARD, "open-252")[0]

    assert "/open-252/j/" in posting.url
    assert WorkableAdapter.matches(posting.url)


def test_workable_location_joins_its_parts() -> None:
    """Location arrives split. Joining is not cosmetic.

    Search filters match on location text, and a posting whose location reads
    only "Amsterdam" fails a filter for the Netherlands.
    """
    from packages.crawler.extract import WorkableExtractor

    posting = WorkableExtractor().parse(_WORKABLE_BOARD, "open-252")[0]

    assert posting.location == "Amsterdam, North Holland, Netherlands"


def test_workable_reads_the_publication_date() -> None:
    from packages.crawler.extract import WorkableExtractor

    posting = WorkableExtractor().parse(_WORKABLE_BOARD, "open-252")[0]

    assert posting.published_at is not None
    assert posting.published_at.year == 2026


def test_workable_description_drops_script_content() -> None:
    from packages.crawler.extract import WorkableExtractor

    posting = WorkableExtractor().parse(_WORKABLE_BOARD, "open-252")[0]

    assert "Support enterprise customers." in (posting.description_raw or "")
    assert "t(1)" not in (posting.description_raw or "")


def test_workable_shares_one_api_host_with_every_other_customer() -> None:
    """§2.6 — a 60s floor here serializes every Workable company."""
    from packages.crawler.ratelimit import MIN_SHARED_API_DELAY_SECONDS, floor_for

    assert floor_for("apply.workable.com") == MIN_SHARED_API_DELAY_SECONDS
