"""A description offers remote work only where it says this role is remote.

Found 2026-11-03 by 500 paraphrased questions: 72 of the 500 postings they were
written from were on-site outside California and kept in the owner's search
area because their description said "remote" somewhere. Of the 1,020 open
postings kept that way, the words were a hybrid schedule ("onsite Tuesdays
through Fridays, with the flexibility to work remotely on Mondays"), a perk
("Remote workspace stipend"), pay fine print ("Maryland, Colorado, Washington
and remote workers"), boilerplate, a scam warning, an FAA "Remote Pilot
Certificate", or a LinkedIn tag. The wording below is from those postings.
"""

from __future__ import annotations

import pytest

from packages.matching.locality import area_exclusion, reads_as_remote

ONSITE_VIRGINIA = "Sterling, Virginia, United States"


@pytest.mark.parametrize(
    "description",
    [
        "Location: Onsite at our office in Sterling, Virginia. Team members work onsite Tuesdays "
        "through Fridays, with the flexibility to work remotely on Mondays.",
        "Location: Hybrid (3 days in office/2 days remote) - New York, NY",
        "Benefits: Remote workspace, internet, and cellphone stipend.",
        "$95,000.00-$110,000.00 for the location of: Maryland, Colorado, Washington and "
        "remote workers",
        "If this role is remote, we have listed the top to the bottom of the possible range.",
        "FAA Part 107 Remote Pilot Certificate, CAPM or PMP certification.",
        "2+ years collaborating with remote teams across geographies and time zones.",
        "Experience managing a remote team spread across multiple regions.",
        "The imposters are conducting remote interviews and making fake job offers.",
        "Annual Work From Anywhere 4 weeks per year.",
        "Salary Range: $100k-120k #LI-Hybrid #LI-Remote",
        "We support remote work for most roles, but if you prefer in-person work, join us.",
    ],
)
def test_a_mention_of_remote_work_is_not_an_offer_of_it(description: str) -> None:
    assert not reads_as_remote(title="Analyst", location=ONSITE_VIRGINIA, description=description)
    assert area_exclusion(
        ONSITE_VIRGINIA, title="Analyst", description=description, remote_outside_california=True
    )


@pytest.mark.parametrize(
    "description",
    [
        "Work location: Remote Travel: May include up to 15% domestic travel.",
        "Location: Remote, National Role",
        "Location: This is a remote position.",
        "This role is remote and can be executed in the United States.",
        "This role is approved to be either Remote within the United States or Hybrid.",
        "Location: This role will be performed at one location (Remote)",
        "Virtual Mobility Classification - Work will be performed from a remote location after "
        "the onboarding period.",
        "Remote: Available for candidates outside these locations.",
        "This role can have a Hybrid or Remote work arrangement.",
        "Affirm is proud to be a remote-first company. Most roles can be done from anywhere.",
        "Commit code from anywhere: Temporal is a fully-remote company.",
        "Three days a week in office if you live near an Okta office. Other locations are remote.",
    ],
)
def test_a_role_declared_remote_still_is(description: str) -> None:
    assert reads_as_remote(title="Analyst", location=ONSITE_VIRGINIA, description=description)
    assert (
        area_exclusion(
            ONSITE_VIRGINIA,
            title="Analyst",
            description=description,
            remote_outside_california=True,
        )
        is None
    )


@pytest.mark.parametrize(
    "description",
    [
        "This role is remote for candidates within approximately 50 miles of Washington, DC.",
        "Location: This is a remote position. Candidates located within 50 miles of our office.",
        "This role is remote, based in Dallas.",
    ],
)
def test_remote_within_one_area_is_not_remote_for_somewhere_else(description: str) -> None:
    assert not reads_as_remote(title="Analyst", location=ONSITE_VIRGINIA, description=description)


def test_remote_within_the_country_is_still_remote() -> None:
    description = "This role is remote, based in the United States, with occasional travel."
    assert reads_as_remote(title="Analyst", location=ONSITE_VIRGINIA, description=description)


def test_a_country_code_beside_a_province_is_canada() -> None:
    """Iron Mountain writes country | region | city: "CA | BC | Burnaby" is Canada."""
    location = "CA | BC | Burnaby | 5566 Trapp Avenue Unit 100"
    assert area_exclusion(
        location, title="Class 5 Driver- Union", description="", remote_outside_california=True
    )


def test_a_state_code_beside_its_country_is_still_california() -> None:
    location = "US | CA | San Jose | 1 Main Street"
    assert (
        area_exclusion(location, title="Driver", description="", remote_outside_california=True)
        is None
    )
