"""Which titles are IT roles: the owner reads only IT postings (2026-11-09)."""

from __future__ import annotations

import pytest

from packages.matching.it_titles import ADJACENT_GROUPS, IT_GROUPS, is_it_role, it_group

#: Illustrative examples from the BLS 2018 SOC computer occupations (15-1211 to
#: 15-1299), 11-3021 and 15-2051, as an employer would post them.
SOC_EXAMPLES = (
    "Information Systems Analyst",
    "Systems Architect",
    "Network Security Analyst",
    "IT Risk Specialist",
    "Network Support Technician",
    "Help Desk Technician",
    "IT Support Specialist",
    "Computer Network Engineer",
    "Database Administrator",
    "Data Architect",
    "Database Developer",
    "Network Analyst",
    "Systems Programmer",
    "Mobile Applications Developer",
    "Software Quality Engineer",
    "Web Applications Developer",
    "Data Center Operator",
    "Business Intelligence Developer",
    "Chief Technology Officer",
)

TODAYS_IT_TITLES = (
    "Software Engineer",
    "Sr. SW Engineer",
    "Senior Manager, Full-stack Engineer",
    "Staff Machine Learning Engineer",
    "Lead Data Engineer",
    "Data Scientist, Professional Senior",
    "Site Reliability Engineer, AI Inference",
    "DevOps Engineer (m/f/d)",
    "Senior Information Security Analyst",
    "ServiceNow Developer",
    "SAP ABAP Developer",
    "Desktop Support Technician",
    "Program Manager (IT)",
    "Director, Software Engineering",
    "Firmware Engineer",
    # Research and analyst roles, owner's note of 2026-11-09.
    "Senior Applied Researcher",
    "Operations Research Engineer",
    "Quantum Topological Qubits Research Scientist",
    "Robotics Researcher",
    "Senior Business System Analyst (Order Management)",
    "Business Analyst - Metrics & Reporting",
    "MDM analyst",
    "Sr. Threat Hunting Intelligence Analyst II",
)

NOT_IT = (
    "(USA) Front End Coach",
    "Security Officer",
    "Loss Prevention Associate",
    "Spark Driver",
    "Sr. Mobile Expert",
    "Mobile Associate, Store-in-Store - Retail Sales",
    "Data Entry Clerk",
    "Provider Network Analyst",
    "API 510 Pressure Vessel Inspector",
    "Cashier",
    "Pharmacy Technician",
    "Mechanical Design Engineer",
    "Sr. Electrical Engineer",
    "Teller",
    "SMB Account Executive – Data Foundation (MuleSoft + Informatica)",
    "Inside Sales Representative, Cloud",
    # "not like sale executive, sale manager", owner, 2026-11-09.
    "Sales Manager - Cloud Platform",
    "Director of Sales, AI Solutions",
    "Business Development Manager, Data Center",
    "Research Associate II - Bioassay",
    "Sr Scientist, Inflammation Biomarkers and Translational Research",
    "Senior Financial Analyst",
    "Senior Analyst, Supply Chain Planning",
    "",
    None,
)


@pytest.mark.parametrize("title", SOC_EXAMPLES + TODAYS_IT_TITLES)
def test_an_it_title_is_an_it_role(title):
    assert it_group(title) in IT_GROUPS
    assert is_it_role(title)


@pytest.mark.parametrize("title", NOT_IT)
def test_a_title_outside_it_is_not_an_it_role(title):
    assert it_group(title) is None
    assert not is_it_role(title)


@pytest.mark.parametrize(
    ("title", "group"),
    [
        ("Lead Product Manager - Technical", "product_program"),
        ("Senior Director, Technical Program Management", "product_program"),
        ("Lead UX Designer", "ux_design"),
        ("Solutions Consultant 2", "tech_sales_consulting"),
        ("Technical Account Manager", "tech_sales_consulting"),
        ("ASIC DFT Engineer", "hardware"),
        ("IC Verification Engineer", "hardware"),
    ],
)
def test_an_it_adjacent_title_is_labelled_but_not_it_unless_named(title, group):
    assert it_group(title) == group
    assert group in ADJACENT_GROUPS
    assert not is_it_role(title)
    assert is_it_role(title, include={group})


def test_an_unknown_adjacent_group_is_refused():
    with pytest.raises(ValueError, match="unknown IT-adjacent groups: pilots"):
        is_it_role("Software Engineer", include={"pilots"})


def test_it_is_a_word_only_in_capitals():
    assert it_group("Make It Happen Associate") is None
    assert it_group("IT Business Lead Analyst, Vice President") in IT_GROUPS
