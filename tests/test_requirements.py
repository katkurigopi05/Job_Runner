"""Structured requirements: what a posting states, with the line it said it in.

The property every test here protects is the same one: an unstated value
stays unstated. A filter excludes on these fields, so a guess — an assumed
annual period, a blurb read as a demand — silently hides a job the owner
would have wanted.
"""

from __future__ import annotations

import pytest

from packages.matching.requirements import Requirement, extract, read_compensation
from packages.matching.skill_vocab import normalize_skill

# --------------------------------------------------------------------------
# Compensation
# --------------------------------------------------------------------------


def test_an_annual_usd_range_is_read_with_its_evidence() -> None:
    pay = read_compensation(
        "About the role\nThe annual base salary range for this role is $150,000 - $200,000 USD."
    )

    assert pay is not None
    assert (pay.minimum, pay.maximum, pay.currency, pay.period) == (
        150_000,
        200_000,
        "USD",
        "year",
    )
    assert "$150,000 - $200,000" in pay.quote


def test_thousands_suffixes_are_expanded() -> None:
    pay = read_compensation("Compensation: $120k–$160k per year")

    assert pay is not None
    assert (pay.minimum, pay.maximum) == (120_000, 160_000)


def test_an_hourly_rate_keeps_its_period() -> None:
    pay = read_compensation("Pay: $45.50 - $60 per hour")

    assert pay is not None
    assert (pay.minimum, pay.maximum, pay.period) == (45.5, 60, "hour")


def test_a_currency_code_outranks_the_dollar_sign() -> None:
    pay = read_compensation("Salary: $90,000 - $110,000 CAD annually")

    assert pay is not None
    assert pay.currency == "CAD"


def test_a_range_without_a_stated_period_is_not_assumed_annual() -> None:
    pay = read_compensation("Salary range: £60,000 to £75,000")

    assert pay is not None
    assert pay.currency == "GBP"
    assert pay.period is None


def test_a_heading_above_the_figure_supplies_the_period() -> None:
    pay = read_compensation("Annual Salary Range\n$130,000—$170,000")

    assert pay is not None
    assert pay.period == "year"


@pytest.mark.parametrize(
    "text",
    [
        "We raised $50M in Series B funding from top investors.",
        "Generous 401(k) matching and a $1,000 learning budget.",
        "Our ARR grew past $100 million last year.",
        "No compensation details here at all.",
    ],
)
def test_figures_that_are_not_pay_are_not_read_as_pay(text: str) -> None:
    assert read_compensation(text) is None


# --------------------------------------------------------------------------
# Skills
# --------------------------------------------------------------------------

POSTING = """About us
We build our platform with Go, React and a lot of coffee.

Requirements
- 3+ years with Python and PostgreSQL
- Experience with Terraform is a plus

Nice to have
- Kubernetes
- Python performance tuning
"""


def test_skills_are_split_by_the_heading_they_sit_under() -> None:
    reading = extract(POSTING)

    required = {m.skill for m in reading.skills_by(Requirement.REQUIRED)}
    preferred = {m.skill for m in reading.skills_by(Requirement.PREFERRED)}
    unclassified = {m.skill for m in reading.skills_by(Requirement.UNCLASSIFIED)}

    assert required == {"python", "postgresql"}
    assert preferred == {"terraform", "kubernetes"}
    assert unclassified == {"go", "react"}


def test_a_line_that_calls_itself_a_plus_is_preferred_under_requirements() -> None:
    terraform = next(m for m in extract(POSTING).skills if m.skill == "terraform")

    assert terraform.requirement is Requirement.PREFERRED
    assert "is a plus" in terraform.quote


def test_required_anywhere_beats_preferred_elsewhere() -> None:
    python = next(m for m in extract(POSTING).skills if m.skill == "python")

    assert python.requirement is Requirement.REQUIRED
    assert "3+ years" in python.quote


def test_ordinary_english_is_not_a_technology() -> None:
    reading = extract(
        "Requirements\n"
        "- Ability to react to incidents and go the extra mile\n"
        "- Excellent swift judgment"
    )

    assert {m.skill for m in reading.skills} == set()


def test_java_is_not_javascript_and_cpp_is_not_c() -> None:
    reading = extract("Requirements\n- JavaScript and C++")

    assert {m.skill for m in reading.skills} == {"javascript", "cpp"}


@pytest.mark.parametrize(
    ("typed", "key"),
    [
        ("postgres", "postgresql"),
        ("PostgreSQL", "postgresql"),
        ("k8s", "kubernetes"),
        ("react", "react"),
    ],
)
def test_a_typed_filter_term_meets_the_posting_on_one_key(typed: str, key: str) -> None:
    assert normalize_skill(typed) == key


def test_an_unknown_filter_term_is_not_silently_mapped() -> None:
    assert normalize_skill("underwater basket weaving") is None


# --------------------------------------------------------------------------
# Education
# --------------------------------------------------------------------------


def test_a_degree_with_an_equivalent_route_records_both() -> None:
    reading = extract(
        "Minimum qualifications\n- BS or MS in Computer Science, or equivalent practical experience"
    )

    assert reading.education is not None
    assert reading.education.level == "bachelor"
    assert reading.education.requirement is Requirement.REQUIRED
    assert reading.education.equivalent_experience is True


def test_a_preferred_degree_does_not_become_a_requirement() -> None:
    reading = extract("Preferred qualifications\n- Master's degree in Statistics")

    assert reading.education is not None
    assert (reading.education.level, reading.education.requirement) == (
        "master",
        Requirement.PREFERRED,
    )


def test_a_required_degree_outranks_a_preferred_higher_one() -> None:
    reading = extract(
        "Requirements\n- Bachelor's degree in Engineering\n\nPreferred\n- PhD in Machine Learning"
    )

    assert reading.education is not None
    assert (reading.education.level, reading.education.requirement) == (
        "bachelor",
        Requirement.REQUIRED,
    )


# --------------------------------------------------------------------------
# The stored shape
# --------------------------------------------------------------------------


def test_silence_is_named_in_the_stored_shape() -> None:
    stored = extract("We are a friendly team.").as_json()

    assert stored["compensation"] is None
    assert stored["education"] is None
    assert set(stored["unknown"]) == {"compensation", "skills", "education"}


def test_a_figure_with_no_period_is_named_as_unknown_period() -> None:
    stored = extract("Salary range: £60,000 to £75,000").as_json()

    assert "pay_period" in stored["unknown"]
    assert "compensation" not in stored["unknown"]


def test_an_empty_posting_reads_as_nothing_known() -> None:
    assert extract(None).as_json()["unknown"] == ["compensation", "skills", "education"]


def test_an_unbulleted_item_line_does_not_reset_the_list_it_is_in() -> None:
    """ "Kubernetes" alone on a line is an item, not a new heading."""
    reading = extract("Nice to have\nKubernetes\nTerraform")

    assert {m.skill for m in reading.skills_by(Requirement.PREFERRED)} == {
        "kubernetes",
        "terraform",
    }
