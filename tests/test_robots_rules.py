"""Reading a robots.txt — CLAUDE.md §2.6, RFC 9309.

§2.6 says the crawler respects robots.txt. Until 2026-10-12 it read the file
with `urllib.robotparser`, which follows the 1994 note the RFC replaced, and
five ordinary file shapes came back "allowed" for a path the site had
disallowed. Each is a test here, and each failed first.

Found by comparison: `tools/robots_check.py` in MadsLorentzen/ai-job-search
(MIT) had already met the byte-order mark and the blank line.

Nothing reaches the network.
"""

from __future__ import annotations

import httpx
import pytest

from packages.crawler.robots import RobotsCache
from packages.crawler.robots_rules import parse

US = "jobrunner"


def allows(body: str | bytes, path: str, agent: str = US) -> bool:
    raw = body if isinstance(body, bytes) else body.encode()
    return parse(raw).allows(agent, f"https://example.com{path}")


# --------------------------------------------------------------------------
# The five shapes that were read as permission
# --------------------------------------------------------------------------

FAILED_OPEN = {
    "a byte-order mark before the first field": (
        b"\xef\xbb\xbfUser-agent: *\nDisallow: /\n",
        "/careers",
    ),
    "a blank line between User-agent and its rules": (
        b"User-agent: *\n\nDisallow: /careers/private\n",
        "/careers/private/1",
    ),
    "Allow: / written before a narrower Disallow": (
        b"User-agent: *\nAllow: /\nDisallow: /careers/private\n",
        "/careers/private/1",
    ),
    "a wildcard in the path": (
        b"User-agent: *\nDisallow: /jobs/*/apply\n",
        "/jobs/123/apply",
    ),
    "an end anchor": (
        b"User-agent: *\nDisallow: /*.json$\n",
        "/api/jobs.json",
    ),
}


@pytest.mark.parametrize("shape", sorted(FAILED_OPEN))
async def test_the_crawler_refuses_what_each_shape_disallows(shape: str) -> None:
    """Through `RobotsCache.check`, which is what the fetcher asks."""
    body, path = FAILED_OPEN[shape]
    cache = RobotsCache(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )

    decision = await cache.check(f"https://example.com{path}")

    assert not decision.allowed, shape
    assert "disallowed" in decision.reason


# --------------------------------------------------------------------------
# Which rule decides
# --------------------------------------------------------------------------


def test_the_longest_match_decides_whatever_the_order() -> None:
    body = "User-agent: *\nAllow: /\nDisallow: /careers/private\n"

    assert not allows(body, "/careers/private/1")
    assert allows(body, "/careers/open/1")


def test_a_longer_allow_unblocks_a_path_under_a_disallow() -> None:
    body = "User-agent: *\nDisallow: /careers\nAllow: /careers/open\n"

    assert allows(body, "/careers/open/1")
    assert not allows(body, "/careers/closed/1")


@pytest.mark.parametrize(
    "body",
    [
        "User-agent: *\nAllow: /careers\nDisallow: /careers\n",
        "User-agent: *\nDisallow: /careers\nAllow: /careers\n",
    ],
)
def test_a_tie_goes_to_disallow(body: str) -> None:
    """RFC 9309 says an equal Allow SHOULD win. A crawler that is asking
    whether it is welcome takes the answer that says no."""
    assert not allows(body, "/careers/1")


def test_an_empty_disallow_allows_everything() -> None:
    assert allows("User-agent: *\nDisallow:\n", "/careers")


def test_a_file_with_no_rules_for_anyone_allows() -> None:
    assert allows("", "/careers")
    assert allows("Sitemap: https://example.com/sitemap.xml\n", "/careers")


def test_a_rule_before_any_user_agent_belongs_to_nobody() -> None:
    assert allows("Disallow: /\n", "/careers")


def test_comments_are_not_part_of_a_rule() -> None:
    body = "# keep out\nUser-agent: *  # everyone\nDisallow: /careers/private # staff\n"

    assert not allows(body, "/careers/private/1")
    assert allows(body, "/careers")


# --------------------------------------------------------------------------
# Wildcards
# --------------------------------------------------------------------------


def test_a_wildcard_matches_any_run_of_characters() -> None:
    body = "User-agent: *\nDisallow: /jobs/*/apply\n"

    assert not allows(body, "/jobs/123/apply")
    assert not allows(body, "/jobs/a/b/apply/form")
    assert allows(body, "/jobs/123")


def test_an_end_anchor_matches_only_at_the_end() -> None:
    body = "User-agent: *\nDisallow: /*.json$\n"

    assert not allows(body, "/api/jobs.json")
    assert allows(body, "/api/jobs.json.html")


def test_a_dollar_that_does_not_end_the_rule_is_a_dollar() -> None:
    body = "User-agent: *\nDisallow: /pay$scale\n"

    assert not allows(body, "/pay$scale/2026")
    assert allows(body, "/pay")


def test_the_query_string_is_matched_with_the_path() -> None:
    body = "User-agent: *\nDisallow: /search?q=\n"

    assert not allows(body, "/search?q=engineer")
    assert allows(body, "/search")


# --------------------------------------------------------------------------
# Whose rules
# --------------------------------------------------------------------------


def test_a_group_naming_us_replaces_the_default() -> None:
    body = "User-agent: *\nDisallow: /\n\nUser-agent: jobrunner\nDisallow: /admin\n"

    assert allows(body, "/careers")
    assert not allows(body, "/admin")


def test_our_name_is_matched_whatever_its_case_or_version() -> None:
    assert not allows("User-agent: JobRunner\nDisallow: /careers\n", "/careers")
    assert not allows("User-agent: jobrunner/2.0\nDisallow: /careers\n", "/careers")
    assert not allows(
        "User-agent: jobrunner\nDisallow: /careers\n", "/careers", agent="JobRunner/1.0 (+local)"
    )


def test_another_crawlers_group_is_not_ours() -> None:
    body = "User-agent: jobrunner-archive\nDisallow: /\n"

    assert allows(body, "/careers")


def test_several_user_agents_share_the_rules_that_follow() -> None:
    body = "User-agent: somebot\nUser-agent: jobrunner\nDisallow: /careers\n"

    assert not allows(body, "/careers")


def test_two_groups_for_one_agent_are_read_together() -> None:
    """RFC 9309 §2.2.1: groups for the same agent are combined."""
    body = "User-agent: *\nDisallow: /a\n\nUser-agent: *\nDisallow: /b\n"

    assert not allows(body, "/a")
    assert not allows(body, "/b")


def test_a_line_that_is_not_a_rule_does_not_end_a_group() -> None:
    body = "User-agent: *\nSitemap: https://example.com/s.xml\nDisallow: /careers/private\n"

    assert not allows(body, "/careers/private")


# --------------------------------------------------------------------------
# Octets, not characters (RFC 9309 §2.2.2)
# --------------------------------------------------------------------------


def test_an_encoded_rule_matches_the_plain_path_and_the_other_way() -> None:
    assert not allows("User-agent: *\nDisallow: /open%20roles\n", "/open roles/1")
    assert not allows("User-agent: *\nDisallow: /open roles\n", "/open%20roles/1")


def test_a_rule_in_utf8_matches_the_encoded_url() -> None:
    assert not allows("User-agent: *\nDisallow: /café\n", "/caf%C3%A9/jobs")


def test_a_byte_that_is_not_utf8_is_matched_as_the_byte_it_is() -> None:
    """Decoded with `errors="replace"`, this rule became `/caf\\ufffd` and
    matched nothing the site serves."""
    assert not allows(b"User-agent: *\nDisallow: /caf\xe9\n", "/caf%E9/jobs")


def test_windows_line_endings_are_line_endings() -> None:
    assert not allows(b"User-agent: *\r\nDisallow: /careers\r\n", "/careers")


# --------------------------------------------------------------------------
# Crawl-delay
# --------------------------------------------------------------------------


def test_crawl_delay_is_ours_before_the_default() -> None:
    body = "User-agent: *\nCrawl-delay: 30\n\nUser-agent: jobrunner\nCrawl-delay: 90\n"

    assert parse(body.encode()).crawl_delay(US) == 90.0
    assert parse(b"User-agent: *\nCrawl-delay: 30\n").crawl_delay(US) == 30.0
    assert parse(b"User-agent: *\nDisallow: /a\n").crawl_delay(US) is None


@pytest.mark.parametrize("value", ["soon", "-5", "inf", "nan", ""])
def test_a_crawl_delay_that_is_not_a_wait_is_ignored(value: str) -> None:
    """`inf` parses as a float, and a delay of forever is a host never
    fetched again by a rule nobody wrote."""
    assert parse(f"User-agent: *\nCrawl-delay: {value}\n".encode()).crawl_delay(US) is None


def test_a_crawl_delay_keeps_the_rules_after_it() -> None:
    body = "User-agent: *\nCrawl-delay: 10\nDisallow: /careers/private\n"

    assert not allows(body, "/careers/private")
    assert parse(body.encode()).crawl_delay(US) == 10.0
