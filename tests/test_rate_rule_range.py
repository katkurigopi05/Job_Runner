"""§2.6 as the owner set it on 2026-11-02: our own delay is 5 to 60 s a host.

5 s is the floor, the default, and what a site gets unless it asks for more.
60 s is the top of what the setting may choose. A site's own Crawl-delay and
a 429 can still ask for more than that, and still win: respecting robots.txt
is its own rule, not a number we pick.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.core.config import Settings
from packages.crawler.fetch import build_fetcher
from packages.crawler.ratelimit import (
    MAX_CONFIGURED_DELAY_SECONDS,
    MIN_DELAY_SECONDS,
    HostRateLimiter,
)


@pytest.mark.parametrize("seconds", [5, 30, 60])
def test_the_setting_takes_anything_from_five_to_sixty(seconds):
    assert Settings(crawler_min_delay_s=seconds).crawler_min_delay_s == seconds


@pytest.mark.parametrize("seconds", [4, 61, 0])
def test_the_setting_refuses_outside_five_to_sixty(seconds):
    with pytest.raises(ValidationError):
        Settings(crawler_min_delay_s=seconds)


def test_the_default_is_the_floor():
    assert Settings.model_fields["crawler_min_delay_s"].default == MIN_DELAY_SECONDS


def test_the_settings_range_is_the_limiters_range():
    bounds = {type(m).__name__: m for m in Settings.model_fields["crawler_min_delay_s"].metadata}
    assert bounds["Ge"].ge == MIN_DELAY_SECONDS
    assert bounds["Le"].le == MAX_CONFIGURED_DELAY_SECONDS == 60


def test_a_fetcher_asked_for_more_than_sixty_is_refused_not_quietly_lowered():
    with pytest.raises(ValueError, match="60"):
        build_fetcher(delay_seconds=MAX_CONFIGURED_DELAY_SECONDS + 1, shared=False)


async def test_a_sites_crawl_delay_may_still_ask_for_more_than_sixty():
    limiter = HostRateLimiter()
    await limiter.raise_delay("careers.acme.example", 600.0)
    assert limiter.delay_for("careers.acme.example") == 600.0
