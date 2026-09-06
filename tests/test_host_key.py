"""One host, one counter — §2.6.

`_last_request` and `_blocked_until` are dicts keyed by a string, so the floor
is only a floor if every spelling of a host produces the same string. These
pin that, and the origin split robots.txt needs instead.
"""

from __future__ import annotations

import pytest

from packages.crawler.ratelimit import HostRateLimiter, floor_for, host_key
from packages.crawler.robots import origin_key

#: Four ways a company list can write one host. All are the same machine.
SPELLINGS = (
    "https://boards-api.greenhouse.io/v1/boards/a/jobs",
    "https://boards-api.greenhouse.io:443/v1/boards/b/jobs",
    "https://BOARDS-API.GREENHOUSE.IO/v1/boards/c/jobs",
    "https://boards-api.greenhouse.io./v1/boards/d/jobs",
    "https://user@boards-api.greenhouse.io/v1/boards/e/jobs",
)


def test_every_spelling_of_one_host_is_one_key() -> None:
    assert len({host_key(url) for url in SPELLINGS}) == 1


def test_a_second_spelling_does_not_buy_a_second_request() -> None:
    """`netloc` gave each spelling its own counter, so the floor was 5x looser.

    Reachable rather than theoretical: `bespoke.probe_page` and
    `find_boards.from_url` take URLs from a company CSV, and
    `resolve.find_embedded` takes them out of an aggregator's own HTML.
    """
    limiter = HostRateLimiter(clock=lambda: 0.0)
    limiter.record(host_key(SPELLINGS[0]))

    for url in SPELLINGS[1:]:
        assert not limiter.is_ready(host_key(url)), url


def test_a_retry_after_binds_every_spelling() -> None:
    """§2.6 permits the 2s shared floor only "while also listening".

    A backoff one spelling of the host can walk around is not listening.
    """
    limiter = HostRateLimiter(clock=lambda: 0.0)
    limiter.penalize(host_key(SPELLINGS[0]), 600)

    for url in SPELLINGS:
        assert limiter.time_until_ready(host_key(url)) == pytest.approx(600.0), url


def test_the_shared_api_floor_survives_an_explicit_port() -> None:
    """`boards-api.greenhouse.io:443` is not in `SHARED_API_HOSTS` as written."""
    assert floor_for(host_key("https://boards-api.greenhouse.io:443/x")) == 2.0


def test_a_company_host_still_gets_the_full_floor() -> None:
    """The normalisation must not accidentally widen membership of the 2s list."""
    assert floor_for(host_key("https://careers.acme.example/jobs")) == 60.0
    assert floor_for(host_key("https://notboards-api.greenhouse.io.evil.example/x")) == 60.0


def test_two_hosts_are_still_two_keys() -> None:
    assert host_key("https://api.lever.co/x") != host_key("https://api.ashbyhq.com/x")


def test_a_malformed_host_is_a_key_rather_than_a_crash() -> None:
    """`urlparse("https://[")` raises, and politeness must not kill a sweep."""
    for url in ("https://[", "https://[::1", "http://[oops]/careers"):
        assert host_key(url), url
        assert origin_key(url), url


class TestRobotsOrigin:
    """robots.txt is scoped to an origin, which is a different question."""

    def test_the_scheme_separates_two_origins(self) -> None:
        """`netloc` omits it, so http borrowed https's verdict, or the reverse."""
        assert origin_key("http://x.example/a") != origin_key("https://x.example/a")

    def test_the_port_is_part_of_the_origin(self) -> None:
        """Unlike `host_key`, which drops it: one server, but two robots.txt."""
        assert origin_key("https://x.example:8443/a") != origin_key("https://x.example/a")

    def test_spelling_still_does_not_split_it(self) -> None:
        """Three entries meant three fetches of one file, free to disagree."""
        assert origin_key("https://X.EXAMPLE./a") == origin_key("https://x.example/a")
