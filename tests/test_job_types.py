"""A job type for every posting title.

The titles below are real ones from the owner's crawl. The rules are ordered
and first match wins, so most of what can go wrong is a general rule firing
before a specific one. Each pair here pins one of those orderings.
"""

from __future__ import annotations

import pytest

from packages.matching.job_types import JOB_TYPES, job_type


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Senior Software Engineer", "Software Engineer"),
        ("Member of Technical Staff (Software Engineer, Data Platform)", "Software Engineer"),
        ("Machine Learning Engineer", "AI / ML Engineer"),
        ("AI Engineer", "AI / ML Engineer"),
        ("Applied AI Engineer, Agents & Automations", "AI / ML Engineer"),
        ("AI Researcher", "Research Scientist / Engineer"),
        ("Research Engineer, Retrieval & Search", "Research Scientist / Engineer"),
        ("Sr. Forward Deployed Engineer (FDE) - Financial Services", "Forward Deployed Engineer"),
        ("Senior Software Engineer, Back-end (Fraud)", "Backend Engineer"),
        ("Product Designer", "Designer"),
        ("Staff Product Manager, AI Builder Experience", "Product Manager"),
        ("Director, Product", "Product Manager"),
        ("Senior Named Account Executive, Buffalo", "Account Executive"),
        ("Business Development Representative, Mid-Market", "Sales Development Rep"),
        ("Technical Account Manager", "Account Manager"),
        ("Senior Counsel, Business Legal", "Legal & Compliance"),
        ("Senior Technical Sourcer II", "Recruiting / People"),
        ("Accounts Payable Analyst", "Finance & Accounting"),
        ("Senior Data Scientist, User Growth", "Data Scientist"),
        ("Child Safety Enforcement Specialist", "Trust & Safety / Policy"),
        ("Sr. Developer Advocate, Open Source", "Developer Relations"),
        ("Principal Enterprise Architect", "Architect"),
        ("Software Engineering Internship - San Francisco", "Intern"),
    ],
)
def test_real_titles_get_their_job_type(title: str, expected: str) -> None:
    assert job_type(title) == expected


@pytest.mark.parametrize(
    ("specific", "general", "expected"),
    [
        # A manager of engineers is not an engineer.
        ("Engineering Manager, AI Models Infrastructure", "Engineer", "Engineering Manager"),
        ("Manager, Field Engineering", "Engineer", "Engineering Manager"),
        # A sales engineer sells; "engineer" alone would file them as building.
        (
            "Sr. Solutions Architect - Financial Services",
            "Software Engineer",
            "Solutions / Sales Engineer",
        ),
        (
            "Senior Value Engineer - Technology Consultant",
            "Software Engineer",
            "Solutions / Sales Engineer",
        ),
        # Product marketing is marketing, not product management.
        ("Senior Technical Product Marketing Manager", "Product Manager", "Product Marketing"),
        ("Technical Program Manager, Partnerships", "Program Manager", "Technical Program Manager"),
    ],
)
def test_a_specific_rule_wins_over_the_general_one(
    specific: str, general: str, expected: str
) -> None:
    assert job_type(specific) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # Each of these was misfiled by a first version of the table.
        ("Software Engineer - New Grad", "Software Engineer"),  # not "Intern"
        ("Software Engineer, Privacy Engineering", "Software Engineer"),  # not "Legal"
        ("Software Engineer, Hardware Health", "Software Engineer"),  # not "Hardware"
        ("Material Planner (Electrical)", "Strategy & Operations"),  # not "Hardware"
        ("Account Associate - EMEA", "Account Manager"),  # not "Technician"
        ("Technical Commodity Manager - Robotics", "Manager (other)"),  # not "Robotics"
    ],
)
def test_the_misfilings_found_on_real_titles_stay_fixed(title: str, expected: str) -> None:
    assert job_type(title) == expected


def test_a_title_nothing_names_is_other_not_a_guess() -> None:
    assert job_type("[US-DC]Kitchen Lead") == "Other"
    assert job_type(None) == "Other"
    assert job_type("") == "Other"


def test_every_label_returned_is_a_declared_type() -> None:
    titles = ["Senior Software Engineer", "Product Designer", "Kitchen Lead", "AI Researcher"]
    assert {job_type(t) for t in titles} <= set(JOB_TYPES)
