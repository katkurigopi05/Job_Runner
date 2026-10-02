"""The search area, measured against location strings boards actually wrote.

Every string here is from the owner's 5,948 open postings on 2026-10-02. They
were run through the two rules then live, the scoring gate and the feed, and
three kinds of fault came out:

- **The rules disagreed** on 107 postings: the gate kept a place it did not
  recognize, and the feed dropped it. Thirteen were "SF Office".
- **A list was read as one place**, so "San Francisco, CA; Pittsburgh, PA;
  Toronto, ON" was dropped as foreign because Ontario was in it.
- **Gaps in the vocabularies**: accents ("Zürich"), "U.S." with a full stop,
  a state code without a comma ("Remote - OR"), and a foreign city beside its
  own country's code where that code is also a state's ("Toronto, CA",
  "Berlin, DE").

After: one rule (`locality.area_exclusion`), each listed office judged on its
own, 95 more postings kept, 3 foreign ones dropped, and 0 disagreements.
"""

from __future__ import annotations

import pytest

from packages.core.models import Posting, Profile
from packages.matching.filters import location_matches
from packages.matching.locality import Locality, area_exclusion, locality_of, locations_in
from packages.matching.search import SearchFilters, matches

KEEP = [
    # Lists with an office in the area.
    "San Francisco, CA; Pittsburgh, PA; Toronto, ON; Dallas, TX",
    "Remote US & Canada; Dallas, TX; Phoenix, AZ; Pittsburgh, PA; San Francisco, CA; Toronto, ON",
    "Remote, Canada; Remote, United States",
    "London; Sunnyvale",
    "New York Office; Remotely in the USA",
    "SF, SEA, NYC, CHI",
    # A remote option beside American offices is remote within the country.
    "Houston, TX or Remote",
    # How boards write San Francisco and the United States.
    "SF Office",
    "SF",
    "Remote-United-States",
    "U.S. Remote",
    "Remote - U.S",
    "USCA",
    # A state code with no comma before it.
    "Remote - OR",
    "Remote - DC",
    "Portland, OR - Remote",
    # A Californian city that is also somewhere else.
    "Ontario, CA",
    # No place, or a region that includes the United States.
    "N/A",
    "Remote, Global",
    "Remote (North America)",
    "Remote",
]

DROP = [
    # A foreign city beside its own country's code, which is also a state's.
    "Toronto, CA",
    "CA-Toronto",
    "Berlin, DE",
    "Tel Aviv, IL",
    "Bangalore, IN",
    "Buenos Aires, AR",
    # Accents, and cities the list did not have.
    "Zürich, CH",
    "Malmö",
    "Frankfurt",
    "CDMX3",
    "CRI - Remote",
    "APJ",
    # Provinces.
    "Ontario - Remote",
    "British Columbia; Ontario",
    # A remote option beside only foreign offices gains nothing.
    "Toronto or Remote",
    # On-site in another state, written several ways.
    "Toronto, ON; Dallas, TX; Pittsburgh, PA",
    "UT - Cottonwood Heights",
    "St. Louis",
    "CHI Office",
    "New York, NY; London; Stockholm",
]


def _excluded(location: str) -> str | None:
    return area_exclusion(
        location, title="Software Engineer", description="", remote_outside_california=True
    )


@pytest.mark.parametrize("location", KEEP)
def test_in_the_area(location: str) -> None:
    assert _excluded(location) is None


@pytest.mark.parametrize("location", DROP)
def test_outside_the_area(location: str) -> None:
    assert _excluded(location) is not None


@pytest.mark.parametrize("location", KEEP + DROP)
def test_the_gate_the_feed_and_the_rule_agree(location: str) -> None:
    """They disagreed on 107 postings while each kept its own copy."""
    posting = Posting(title="Software Engineer", location=location, description_raw="")
    profile = Profile(location="San Francisco, CA, USA")
    feed = matches(posting, SearchFilters(us_only=True)).kept

    assert location_matches(profile, posting) == feed == (_excluded(location) is None)


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("SF Office", Locality.BAY_AREA),
        ("U.S. Remote", Locality.UNITED_STATES),
        ("Remote - OR", Locality.UNITED_STATES),
        ("Wichita Metro Area", Locality.UNITED_STATES),
        ("Toronto, CA", Locality.ELSEWHERE),
        ("Berlin, DE", Locality.ELSEWHERE),
        ("Zürich", Locality.ELSEWHERE),
        ("N/A", Locality.UNKNOWN),
        ("LOCATION", Locality.UNKNOWN),
        # Must not move: real American places that share a name or a code.
        ("Dublin, CA", Locality.CALIFORNIA),
        ("Paris, TX", Locality.UNITED_STATES),
        ("Vancouver, WA", Locality.UNITED_STATES),
        ("Portland, OR", Locality.UNITED_STATES),
        ("Indiana", Locality.UNITED_STATES),
    ],
)
def test_the_misreads_are_read(location: str, expected: Locality) -> None:
    assert locality_of(location) is expected


def test_a_list_is_split_on_its_separators_and_not_on_oregon() -> None:
    assert locations_in("Toronto or New York City") == ["Toronto", "New York City"]
    assert locations_in("London; Sunnyvale") == ["London", "Sunnyvale"]
    assert locations_in("Portland, OR - Remote") == ["Portland, OR - Remote"]
    assert locations_in("San Jose, CA") == ["San Jose, CA"]


def test_strict_drops_what_it_cannot_place() -> None:
    """`allow_unknown_location=False` is the feed's "located postings only"."""

    def strict(location: str) -> str | None:
        return area_exclusion(
            location,
            title=None,
            description=None,
            remote_outside_california=True,
            allow_unknown_location=False,
        )

    assert strict("Remote (North America)") == "location not recognized"
    assert strict("Remote") == "no location given"
    assert strict("San Francisco, CA") is None
