"""Reading a country out of a location string.

The owner's search is United States only, California first. Everything that
makes that hard is an ambiguity in two letters or a substring that matches the
wrong word, so those are what this pins.
"""

from __future__ import annotations

import pytest

from packages.matching.locality import Locality, is_domestic, locality_of, rank

# --------------------------------------------------------------------------
# The two-letter problems
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "San Francisco, CA",
        "San Jose, CA, USA",
        "Palo Alto, CA 94301",
        "Remote - CA",
    ],
)
def test_ca_is_california_when_nothing_says_canada(location: str) -> None:
    assert locality_of(location) in {Locality.BAY_AREA, Locality.CALIFORNIA}


@pytest.mark.parametrize(
    "location",
    [
        "Toronto, ON, CA",
        "Vancouver, BC, CA",
        "Toronto, Canada",
        "Montreal, QC",
        "Canada",
    ],
)
def test_ca_is_canada_when_something_says_so(location: str) -> None:
    """The same two letters, and the country has to be decided first."""
    assert locality_of(location) is Locality.ELSEWHERE


def test_vancouver_washington_is_not_vancouver_canada() -> None:
    """Why a state code outranks a city name.

    Vancouver is a Canadian city and Washington is an American state. Reading
    the city first sends a Washington posting to Canada.
    """
    assert is_domestic(locality_of("Vancouver, WA"))
    assert locality_of("Vancouver, BC") is Locality.ELSEWHERE


# --------------------------------------------------------------------------
# Substrings that match the wrong word
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    ["Indianapolis, IN", "Bloomington, IN", "Independence, MO", "Indiana"],
)
def test_indiana_is_not_india(location: str) -> None:
    """`"ind" in location` drops three American places as Indian ones.

    This is not hypothetical — it is what the first hand-written filter for
    this search did, and it is why every check here is word-boundaried.
    """
    assert is_domestic(locality_of(location))


@pytest.mark.parametrize(
    "location",
    ["Remote or Hybrid", "Hybrid in office", "Home or anywhere", "Onsite ok"],
)
def test_english_words_are_not_state_codes(location: str) -> None:
    """OR, IN, OK, HI, ME, DE and LA are all ordinary words.

    A bare two-letter match reads "Remote or Hybrid" as Oregon and "Onsite ok"
    as Oklahoma. Requiring the comma an American address puts before the state
    is what stops it. Note the limit of the trick: a state *name* in the same
    sentence is still a state, so "Maine or elsewhere" is correctly US.
    """
    assert locality_of(location) is not Locality.UNITED_STATES


# --------------------------------------------------------------------------
# The tiers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    ["San Francisco, CA", "Mountain View, California", "Oakland", "Bay Area", "Sunnyvale, CA"],
)
def test_the_bay_area_is_its_own_tier(location: str) -> None:
    assert locality_of(location) is Locality.BAY_AREA


@pytest.mark.parametrize("location", ["Los Angeles, CA", "San Diego", "Sacramento, California"])
def test_the_rest_of_california(location: str) -> None:
    assert locality_of(location) is Locality.CALIFORNIA


@pytest.mark.parametrize(
    "location", ["Austin, TX", "New York, NY", "Seattle, Washington", "Remote - US", "Mapbox US"]
)
def test_the_rest_of_the_united_states(location: str) -> None:
    assert locality_of(location) is Locality.UNITED_STATES


def test_the_feed_order_is_bay_area_then_california_then_the_country() -> None:
    order = [
        locality_of("San Francisco, CA"),
        locality_of("San Diego, CA"),
        locality_of("Austin, TX"),
        locality_of(None),
        locality_of("Bangalore, India"),
    ]
    assert [rank(item) for item in order] == sorted(rank(item) for item in order)


# --------------------------------------------------------------------------
# Silence versus an unrecognized name
# --------------------------------------------------------------------------


@pytest.mark.parametrize("location", [None, "", "   "])
def test_no_location_is_unknown_not_foreign(location: str | None) -> None:
    """Silence is not evidence. A blank location is not a reason to hide a job."""
    assert locality_of(location) is Locality.UNKNOWN


@pytest.mark.parametrize("location", ["Mapbox Atlantis", "Somewhere Else Entirely"])
def test_an_unrecognized_place_name_is_unplaced_not_unknown(location: str) -> None:
    """An unplaceable string is where foreign postings land.

    Across the 66 postings of the first real board sweep, every string this
    module could not place was foreign. That is why it is a separate class
    from "no location given" and why `us_only` drops it.
    """
    assert locality_of(location) is Locality.UNPLACED
    assert not is_domestic(locality_of(location))


# --------------------------------------------------------------------------
# Postings that name more than one place
# --------------------------------------------------------------------------


def test_a_posting_offering_a_us_office_among_others_is_reachable() -> None:
    """ "Remote - US or Canada" is a job the owner can take."""
    assert is_domestic(locality_of("Bellevue, Washington, USA; San Jose, California, USA"))
    assert is_domestic(locality_of("Austin, TX / London"))
    assert is_domestic(locality_of("Remote - US or Canada"))


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("Newark, NJ", Locality.UNITED_STATES),
        ("Newark, CA", Locality.CALIFORNIA),
        ("Richmond, VA", Locality.UNITED_STATES),
        ("Richmond, CA", Locality.BAY_AREA),
        ("Cambridge, MA", Locality.UNITED_STATES),
        ("Manchester, NH", Locality.UNITED_STATES),
    ],
)
def test_a_state_code_outranks_a_shared_city_name(location: str, expected: Locality) -> None:
    """Newark and Richmond are Bay Area cities *and* famous cities elsewhere.

    Reading the city first files a New Jersey posting under San Francisco.
    Same rule as `Vancouver, WA`: the state is the half that means something.
    """
    assert locality_of(location) is expected


@pytest.mark.parametrize("location", ["Austin", "Seattle", "Boston", "Denver", "Chicago"])
def test_a_bare_us_city_still_places(location: str) -> None:
    """Boards write "Austin" far more often than "Austin, TX".

    Without this the commonest form of American location is unplaceable and a
    US-only search throws away most of its own results.
    """
    assert locality_of(location) is Locality.UNITED_STATES


def test_domestic_is_exactly_the_three_us_tiers() -> None:
    assert is_domestic(Locality.BAY_AREA)
    assert is_domestic(Locality.CALIFORNIA)
    assert is_domestic(Locality.UNITED_STATES)
    assert not is_domestic(Locality.UNKNOWN)
    assert not is_domestic(Locality.UNPLACED)
    assert not is_domestic(Locality.ELSEWHERE)
