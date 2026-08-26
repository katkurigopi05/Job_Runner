"""The filters that looked like they worked.

Asking for a software engineering internship in California returned Staff and
Senior roles, and a Denver posting. Three separate faults, each of which made a
filter appear to run while narrowing almost nothing — which is worse than a
filter that errors, because the results look like an answer.
"""

from __future__ import annotations

import pytest

from packages.core.models import Posting
from packages.matching.search import SearchFilters, detect_seniority, matches


def _posting(title: str = "Software Engineer", location: str = "", body: str = "") -> Posting:
    return Posting(url="https://x.test/1", title=title, location=location, description_raw=body)


# --------------------------------------------------------------------------
# Seniority
# --------------------------------------------------------------------------

INTERN_ONLY = SearchFilters(min_seniority="intern", max_seniority="intern")


def test_a_staff_role_is_not_an_internship() -> None:
    """The headline symptom."""
    verdict = matches(_posting(title="Staff Software Engineer, AI Search"), INTERN_ONLY)

    assert not verdict.kept


def test_an_internship_survives_an_internship_filter() -> None:
    verdict = matches(_posting(title="Software Engineer Intern (Winter 2027)"), INTERN_ONLY)

    assert verdict.kept


def test_a_title_that_does_not_say_is_dropped_not_kept() -> None:
    """The fault that let 55% of the corpus through every seniority filter.

    `matches()` only rejected when the rung was *detected and out of range*, so
    anything unreadable passed. Asking for interns and being shown every
    posting whose title is silent is not a filter at all.
    """
    verdict = matches(_posting(title="Software Engineer"), INTERN_ONLY)

    assert not verdict.kept
    assert any("not stated" in reason for reason in verdict.reasons)


def test_the_owner_can_widen_it_back_out() -> None:
    """Strict by default, not immovable — `allow_unknown_location`'s mirror."""
    verdict = matches(
        _posting(title="Software Engineer"),
        SearchFilters(min_seniority="intern", max_seniority="intern", allow_unknown_seniority=True),
    )

    assert verdict.kept


def test_prose_in_the_description_no_longer_sets_the_rung() -> None:
    """ "lead a team" and "our staff" filed 54% of the corpus as staff.

    Seniority is read from the title, which is what this function's own
    docstring always claimed. A body that happens to use the word is not a
    statement about the job's level.
    """
    posting = _posting(
        title="Software Engineer Intern",
        body="You will lead a team and work with staff engineers and the architect.",
    )

    assert detect_seniority(posting.title or "") == "intern"
    assert matches(posting, INTERN_ONLY).kept


def test_an_internal_tools_role_is_not_an_intern() -> None:
    """`\\bintern\\b` must not fire on "internal"."""
    assert detect_seniority("Senior Engineer, Internal Tools") == "senior"


# --------------------------------------------------------------------------
# Location
# --------------------------------------------------------------------------

IN_CA = SearchFilters(locations=("CA",))


@pytest.mark.parametrize(
    "location",
    ["San Francisco, CA", "Palo Alto, CA, US", "Sunnyvale, CA / Bellevue, WA", "Remote — CA"],
)
def test_california_locations_match(location: str) -> None:
    assert matches(_posting(location=location), IN_CA).kept


@pytest.mark.parametrize("location", ["Toronto, Canada", "Vancouver, Canada", "Carlsbad"])
def test_a_substring_is_not_a_place(location: str) -> None:
    """`"ca" in "canada"` is true, which is how a CA search returned Canada.

    "Carlsbad" is the same fault without a border: the letters are there and
    the place is not.
    """
    assert not matches(_posting(location=location), IN_CA).kept


@pytest.mark.parametrize(
    "location",
    ["San Francisco, California", "Mountain View, California", "california"],
)
def test_the_full_state_name_matches_the_code(location: str) -> None:
    """Narrowing must not cost half of California.

    Employers write both forms and this corpus contains both side by side. A
    word-boundary test alone rejects "Canada" — correct — and also rejects
    "California", which was never the problem.
    """
    assert matches(_posting(location=location), IN_CA).kept


def test_the_code_matches_the_full_state_name() -> None:
    """And the other direction, so it does not matter which the owner typed."""
    in_california = SearchFilters(locations=("California",))
    assert matches(_posting(location="San Jose, CA"), in_california).kept
    assert not matches(_posting(location="Toronto, Canada"), in_california).kept


@pytest.mark.parametrize("location", ["Toronto, ON, CA", "Vancouver, BC, CA"])
def test_the_canadian_country_code_is_not_california(location: str) -> None:
    """The half word boundaries cannot fix, and `locality.py`'s opening warning.

    "CA is two countries": in `San Jose, CA` and `Toronto, ON, CA` alike, "CA"
    is a standalone token, so no amount of anchoring separates them. A state
    search therefore also requires the location to read as domestic, which
    `locality_of` already decides correctly — rather than this module inventing
    a second, worse answer to a question solved next door.
    """
    assert not matches(_posting(location=location), IN_CA).kept


@pytest.mark.parametrize(
    "location",
    ["CA", "Remote — CA", "Palo Alto, CA, US", "Sunnyvale, CA / Bellevue, WA"],
)
def test_domestic_shorthand_still_matches(location: str) -> None:
    """Requiring a domestic reading must not cost the ordinary spellings.

    `locality_of` is case-sensitive about state codes, so an earlier version of
    this check lowered the location first and made *every* state search look
    foreign — narrowing all the way to nothing while looking like it worked.
    """
    assert matches(_posting(location=location), IN_CA).kept


def test_a_multi_office_posting_matches_on_any_one_of_them() -> None:
    """A California office is a California job, whatever else is listed."""
    location = "Dallas, Texas; San Francisco, California; Vancouver, Canada"
    assert matches(_posting(location=location), IN_CA).kept


def test_non_state_searches_are_not_restricted_to_the_us() -> None:
    """The domestic requirement applies to state terms only.

    Searching for "Toronto" should find Toronto. The rule exists to disambiguate
    a two-letter code, not to make the filter refuse foreign places by name.
    """
    in_toronto = SearchFilters(locations=("Toronto",))
    assert matches(_posting(location="Toronto, ON, CA"), in_toronto).kept


def test_longer_place_names_still_match_inside_a_string() -> None:
    """Word boundaries, not exact equality — locations are written freely."""
    in_boston = SearchFilters(locations=("boston",))
    assert matches(_posting(location="Greater Boston Area"), in_boston).kept
