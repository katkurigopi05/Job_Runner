"""The owner's area read strictly: any California office, else remote in the United States.

What gets Qwen vectors (owner, 2026-11-05): "california but remote or hybrid
which we can accept but out of california we need remote". The search area
keeps a location it cannot read; this does not, because the vectors are the
expensive part. Except Workday's "2 Locations": the owner, the same day, "if
job show multiple location they will accept any location".
"""

from __future__ import annotations

import pytest

from packages.matching.locality import california_or_us_remote, names_california_office

REMOTE_BODY = "This role is fully remote."


@pytest.mark.parametrize(
    ("title", "location", "body"),
    [
        ("Data Engineer", "San Francisco, CA", ""),
        ("Data Engineer", "Hybrid - Oakland, CA", ""),
        ("Data Engineer", "Los Angeles, CA (Remote)", ""),
        ("Data Engineer", "Austin, TX; San Jose, CA", ""),
        ("Data Engineer (San Francisco)", "Hybrid", ""),
        ("Data Engineer", "Remote", ""),
        ("Data Engineer", "Remote - US", ""),
        ("Data Engineer", "United States (Remote)", ""),
        ("Data Engineer", "Austin, TX (Remote)", ""),
        ("Data Engineer", "Houston, TX or Remote", ""),
        ("Data Engineer", "Denver, CO", REMOTE_BODY),
        ("Data Engineer", None, REMOTE_BODY),
        ("Data Engineer", "2 Locations", ""),
        ("Data Engineer", "12 Locations", REMOTE_BODY),
    ],
)
def test_california_in_any_mode_and_us_remote_are_in(title, location, body):
    assert california_or_us_remote(title=title, location=location, description=body)


@pytest.mark.parametrize(
    ("title", "location", "body"),
    [
        ("Data Engineer", "Austin, TX", ""),
        ("Data Engineer", "Hybrid - Seattle, WA", ""),
        ("Data Engineer", "United States", ""),
        ("Data Engineer", None, ""),
        ("Data Engineer", "PH, Taguig", ""),
        ("Data Engineer", "Toronto, ON", ""),
        ("Data Engineer", "Remote - EMEA", ""),
        ("Data Engineer", "CA | BC | Burnaby", ""),
        ("Data Engineer (San Francisco)", "Austin, TX", ""),
    ],
)
def test_out_of_state_on_site_abroad_and_unreadable_are_out(title, location, body):
    assert not california_or_us_remote(title=title, location=location, description=body)


def test_a_canadian_address_is_not_a_california_office():
    assert names_california_office("San Diego, CA", title=None)
    assert not names_california_office("CA | BC | Burnaby", title=None)
    assert not names_california_office("Toronto, ON, CA", title=None)
