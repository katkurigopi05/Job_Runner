"""Reading a country out of a location string.

The owner's search is United States only, California first. Everything that
makes that hard is an ambiguity in two letters or a substring that matches the
wrong word, so those are what this pins.
"""

from __future__ import annotations

import pytest

from packages.matching import locality as _locality
from packages.matching.locality import (
    Locality,
    is_domestic,
    locality_of,
    names_no_place,
    onsite_ok,
    rank,
    reachable,
    reads_as_remote,
)

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


# --------------------------------------------------------------------------
# A working mode is not a place
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    ["Remote", "remote", "REMOTE", "(Remote)", "Remote -", "Hybrid / Flexible", "Anywhere"],
)
def test_a_mode_only_location_names_no_place(location: str) -> None:
    assert names_no_place(location)
    assert locality_of(location) is Locality.UNKNOWN


@pytest.mark.parametrize(
    "location", ["Remote - EMEA", "Remote — CA", "Canada - Remote (ON)", "Remote, USA"]
)
def test_a_mode_word_beside_a_place_still_places(location: str) -> None:
    """Only a string that is *entirely* mode words names no place."""
    assert not names_no_place(location)
    assert locality_of(location) is not Locality.UNKNOWN


def test_bare_remote_is_unknown_rather_than_unplaced() -> None:
    """`UNPLACED` means an unrecognized place *name*, and the corpus says those
    are foreign — so landing "Remote" there dropped the commonest way a
    domestic board writes the job the owner most wants."""
    assert locality_of("Remote") is Locality.UNKNOWN
    assert locality_of("Mapbox Minsk") is not Locality.UNKNOWN


# --------------------------------------------------------------------------
# One definition of remote
# --------------------------------------------------------------------------


def test_a_location_or_title_declaring_remote_is_remote() -> None:
    assert reads_as_remote(title="Backend Engineer", location="Remote - US")
    assert reads_as_remote(title="Backend Engineer (Remote)", location="Austin, TX")
    assert not reads_as_remote(title="Backend Engineer", location="Austin, TX")


def test_distributed_systems_is_not_remote_work() -> None:
    """The false positive that this vocabulary split exists for.

    "distributed" was matched in the body, so any description mentioning
    distributed systems — most backend postings — read as offering remote
    work. Under the search-area rule that turned on-site roles in every state
    into reachable ones.
    """
    assert not reads_as_remote(
        title="Senior Backend Engineer",
        location="Chicago, IL",
        description="Python, PostgreSQL, distributed systems at scale.",
    )
    assert reads_as_remote(
        title="Senior Backend Engineer",
        location="Chicago, IL",
        description="We are a fully distributed team.",
    )


def test_prose_remote_counts_but_an_on_site_marker_overrides_it() -> None:
    assert reads_as_remote(
        title="Backend Engineer", location="Austin, TX", description="This role is remote."
    )
    assert not reads_as_remote(
        title="Backend Engineer (Hybrid)",
        location="Austin, TX",
        description="Some remote work is possible.",
    )


# --------------------------------------------------------------------------
# The search area
# --------------------------------------------------------------------------


def test_onsite_is_california_only() -> None:
    assert onsite_ok(Locality.BAY_AREA)
    assert onsite_ok(Locality.CALIFORNIA)
    assert not onsite_ok(Locality.UNITED_STATES)
    assert not onsite_ok(Locality.ELSEWHERE)


@pytest.mark.parametrize("remote", [True, False])
def test_california_is_reachable_either_way(remote: bool) -> None:
    assert reachable(Locality.BAY_AREA, remote=remote)
    assert reachable(Locality.CALIFORNIA, remote=remote)


def test_the_rest_of_the_us_is_reachable_only_remotely() -> None:
    assert reachable(Locality.UNITED_STATES, remote=True)
    assert not reachable(Locality.UNITED_STATES, remote=False)


@pytest.mark.parametrize("remote", [True, False])
def test_abroad_is_never_reachable(remote: bool) -> None:
    """Remoteness must not override the region: a remote job the owner is not
    eligible to hold is still one they cannot hold."""
    assert not reachable(Locality.ELSEWHERE, remote=remote)
    assert not reachable(Locality.UNPLACED, remote=remote)


@pytest.mark.parametrize("remote", [True, False])
def test_a_posting_naming_no_place_is_kept(remote: bool) -> None:
    """Silence is not evidence of a foreign office."""
    assert reachable(Locality.UNKNOWN, remote=remote)


# --------------------------------------------------------------------------
# The hand-written city lists
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "city",
    [
        "Santa Ana",
        "Stockton",
        "Chula Vista",
        "Modesto",
        "Oxnard",
        "Palmdale",
        "Salinas",
        "Escondido",
        "Visalia",
        "Fullerton",
        "Thousand Oaks",
        "Simi Valley",
        "Victorville",
        "Vallejo",
        "Temecula",
        "Downey",
        "Inglewood",
        "El Cajon",
        "Compton",
        "Redding",
        "Chico",
        "Newport Beach",
        "Alhambra",
        "Napa",
        "Encinitas",
        "Petaluma",
        "Palm Springs",
        "Poway",
        "Los Gatos",
        "Beverly Hills",
        "Manhattan Beach",
        "La Mesa",
        "Oceanside",
        "Menifee",
        "Santa Rosa",
    ],
)
def test_a_bare_california_city_places_in_california(city: str) -> None:
    """The area rule only reaches on-site into California, so a Californian
    city that does not classify is an on-site job silently dropped — the
    costliest direction of error for this owner's search."""
    assert locality_of(city) in {Locality.CALIFORNIA, Locality.BAY_AREA}


@pytest.mark.parametrize(
    "city", ["Frisco", "Bentonville", "Cary", "Greensboro", "Kirkland", "Tysons", "Littleton"]
)
def test_a_bare_us_city_places_in_the_united_states(city: str) -> None:
    """`UNPLACED` is dropped even when remote, so a missing US city costs a
    remote job the owner would take."""
    assert locality_of(city) is Locality.UNITED_STATES


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("Manhattan, NY", Locality.UNITED_STATES),
        ("Mesa, AZ", Locality.UNITED_STATES),
        ("Pasadena, TX", Locality.UNITED_STATES),
        ("Glendale, AZ", Locality.UNITED_STATES),
        ("Riverside, IL", Locality.UNITED_STATES),
        ("Dublin, Ireland", Locality.ELSEWHERE),
        ("Ontario, Canada", Locality.ELSEWHERE),
    ],
)
def test_widening_the_city_lists_did_not_claim_other_places(
    location: str, expected: Locality
) -> None:
    """ "Manhattan Beach" and "La Mesa" have to beat the bare US-city rule
    without dragging Manhattan or Mesa into California with them."""
    assert locality_of(location) is expected


@pytest.mark.parametrize("city", ["Dublin", "Ontario", "Orange", "Fairfield", "Brentwood"])
def test_an_ambiguous_name_needs_the_state_code(city: str) -> None:
    """Deliberately absent from the California list: the bare word names
    somewhere else at least as often, and a false positive puts an out-of-state
    or foreign role in a feed whose on-site tier means California. Writing
    ", CA" resolves every one of them."""
    assert locality_of(city) not in {Locality.CALIFORNIA, Locality.BAY_AREA}
    assert locality_of(f"{city}, CA") is Locality.CALIFORNIA


def test_no_city_is_claimed_by_two_lists() -> None:
    """A name in both a California list and the US list is unreachable in the
    second, because California is checked first — so it reads as a rule that
    does something when it does not. This caught a bad de-duplication that
    moved `richmond` and `santa rosa` out of California entirely.
    """
    californian = set(_locality._BAY_AREA_CITIES) | set(_locality._CALIFORNIA_CITIES)
    assert not (californian & set(_locality._US_CITIES))
    assert not (californian & set(_locality._AMBIGUOUS_CALIFORNIA_CITIES))
