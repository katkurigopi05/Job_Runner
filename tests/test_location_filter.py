"""The hard location filter, after it stopped being a substring test.

`location_matches` split the profile's location on commas and asked whether
any part appeared anywhere in the posting's. On the owner's real profile —
`san fransico , ca,usa` — the two-letter state code did the damage: `ca` is
inside `canada`, `costa rica` and `vancouver`, and inside none of
`united states`. Every Canadian role passed as Californian while American ones
were dropped.

It was not hypothetical. After a real crawl of the 119-company registry the top
of the match feed was four Elastic roles in Canada and a finance manager in
Costa Rica.

`packages/matching/locality.py` was written for exactly this and had never been
wired in. Its own docstring names the trap: "CA is two countries", so the
country has to be settled before `CA` can be read as California.
"""

from __future__ import annotations

import pytest

from packages.core.models import Posting, Profile
from packages.matching.filters import location_matches

#: The owner's profile, spelled correctly.
OWNER = "San Francisco, CA, USA"


def _posting(location: str, description: str = "") -> Posting:
    return Posting(location=location, description_raw=description)


def _keeps(location: str, profile_location: str = OWNER) -> bool:
    return location_matches(Profile(location=profile_location), _posting(location))


# --------------------------------------------------------------------------
# The bug
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "Canada",
        "Vancouver, Canada",
        "Toronto, ON, CA",
        "Costa Rica",
        "San Jose, Costa Rica",
    ],
)
def test_ca_no_longer_matches_canada_or_costa_rica(location: str) -> None:
    """Each of these passed the old filter because `ca` is a substring."""
    assert not _keeps(location)


def test_san_jose_is_read_by_its_country_not_its_city() -> None:
    """The same city name on both sides of a border."""
    assert _keeps("San Jose, CA")
    assert not _keeps("San Jose, Costa Rica")


@pytest.mark.parametrize(
    "location",
    ["United States - Remote", "Remote - USA", "United States"],
)
def test_american_postings_are_kept(location: str) -> None:
    """The other half of the bug: these were *rejected* by the old filter."""
    assert _keeps(location)


# --------------------------------------------------------------------------
# Country is the filter; region is a ranking question
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    ["New York, NY", "Austin, TX", "Portland, OR", "Seattle, WA", "Chicago, IL"],
)
def test_a_us_posting_outside_california_is_not_excluded(location: str) -> None:
    """`locality.rank` orders Bay Area first; this filter only asks the country.

    Excluding here would hide a Texan posting the owner would love in order to
    keep a Californian one they would not.
    """
    assert _keeps(location)


# --------------------------------------------------------------------------
# American places whose names are also countries
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    ["Savannah, Georgia", "Atlanta, GA", "Panama City, FL", "Lebanon, PA", "Lebanon, NH"],
)
def test_us_places_named_after_countries_are_kept(location: str) -> None:
    """Why four country names are deliberately absent from `_NON_US_MARKERS`.

    Rule 1 in `locality_of` runs before any US state *name* is read, so adding
    `georgia` would send `Savannah, Georgia` abroad. A comma-position state
    code rescues `Atlanta, GA`; a spelled-out state name does not.
    """
    assert _keeps(location)


# --------------------------------------------------------------------------
# What still passes
# --------------------------------------------------------------------------


def test_remote_always_qualifies() -> None:
    assert location_matches(Profile(location=OWNER), _posting("Remote"))


def test_an_unplaceable_location_is_not_hidden() -> None:
    """Silence is not evidence, and this is a hard filter.

    A city no rule recognizes should be ranked down, not hidden. Only an
    explicit foreign signal excludes.
    """
    assert _keeps("Wakanda City")


def test_a_missing_location_on_either_side_does_not_exclude() -> None:
    assert _keeps("")
    assert _keeps("Berlin, Germany", profile_location="")


def test_a_profile_outside_the_us_gets_no_opinion() -> None:
    """`locality.py` answers one owner's question — US only, California first.

    A profile located elsewhere gets no verdict from this filter rather than a
    confidently wrong one.
    """
    assert location_matches(Profile(location="Berlin, Germany"), _posting("Toronto, ON"))


# --------------------------------------------------------------------------
# "Remote" is not a place
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "Spain (Remote)",
        "United Kingdom (Remote)",
        "Republic of Ireland (Remote)",
        "Remote - Canada",
        "Kitchener-Waterloo, ON; Remote",
        "Remote, Germany",
    ],
)
def test_remote_inside_another_country_is_still_that_country(location: str) -> None:
    """`is_remote` used to short-circuit before the country was read.

    On a foreign posting "Remote" does not mean remote-from-anywhere; it means
    remote *within that country*. The three highest-scoring matches in the
    owner's feed, once the substring bug was fixed, were Grafana Labs roles in
    Spain, Ireland and the UK — all kept by that short-circuit.
    """
    assert not _keeps(location)


@pytest.mark.parametrize(
    "location",
    ["Remote - US or Canada", "Remote - USA", "U.S. Remote", "Remote - United States", "Remote"],
)
def test_us_remote_is_kept_including_when_it_names_canada_too(location: str) -> None:
    """`locality_of` yields a foreign country name to an explicit US signal.

    `Remote - US or Canada` is a job the owner can take, and must survive the
    check that excludes `Remote - Canada`.
    """
    assert _keeps(location)


# --------------------------------------------------------------------------
# Continents and blocs
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "Europe",
        "Europe (Remote)",
        "Asia",
        "Middle East",
        "South America",
        "Latin America",
        "Nordics",
    ],
)
def test_a_continent_is_not_a_country_but_is_still_abroad(location: str) -> None:
    """Two Synthesia roles located simply `Europe` sat in the owner's top ten.

    The country list had just been extended and a continent is not a country,
    so nothing was reading it.
    """
    assert not _keeps(location)


@pytest.mark.parametrize(
    "location",
    ["Worldwide", "Global", "Anywhere", "International", "North America", "Americas"],
)
def test_open_to_everyone_is_not_excluded(location: str) -> None:
    """These describe a role the owner can take as much as anyone.

    Excluding them would drop jobs that are genuinely available, which is the
    opposite of the bug this filter was fixed for.
    """
    assert _keeps(location)
