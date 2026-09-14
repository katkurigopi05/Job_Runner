"""Feed filters on structured requirements: an unknown is never a pass.

Each filter here excludes, so each has the same two tests: a posting that
states a failing value is dropped, and a posting that states *nothing* is
dropped too unless the owner widened that one filter. The reason string is
asserted as well, because a hidden job with no explanation is how a filter
stops being trusted.
"""

from __future__ import annotations

from datetime import UTC, datetime

from packages.core.models import Posting
from packages.matching.requirements import extract
from packages.matching.search import SearchFilters, matches

NOW = datetime(2026, 9, 14, tzinfo=UTC)


def _posting(text: str | None, *, extracted: bool = True) -> Posting:
    reading = extract(text)
    pay = reading.compensation
    return Posting(
        url="https://example.test/job",
        title="Backend Engineer",
        location=None,
        description_raw=text,
        first_seen_at=NOW,
        closed_at=None,
        salary_min=pay.minimum if (pay and extracted) else None,
        salary_max=pay.maximum if (pay and extracted) else None,
        salary_currency=pay.currency if (pay and extracted) else None,
        salary_period=pay.period if (pay and extracted) else None,
        requirements_json=reading.as_json() if extracted else None,
    )


PAID = "Compensation\nThe annual base salary range is $140,000 - $180,000 USD."
UNPAID = "We are a friendly team."


def _verdict(posting: Posting, **filters: object):
    return matches(posting, SearchFilters(**filters))


# --------------------------------------------------------------------------
# Pay
# --------------------------------------------------------------------------


def test_a_range_reaching_the_floor_is_kept() -> None:
    assert _verdict(_posting(PAID), min_salary=170_000).kept


def test_a_range_below_the_floor_says_where_it_tops_out() -> None:
    verdict = _verdict(_posting(PAID), min_salary=200_000)

    assert not verdict.kept
    assert "tops out at 180,000" in verdict.reasons[0]


def test_unstated_pay_does_not_meet_a_floor() -> None:
    verdict = _verdict(_posting(UNPAID), min_salary=100_000)

    assert not verdict.kept
    assert verdict.reasons == ["pay is not stated"]


def test_unstated_pay_is_kept_only_when_asked_for() -> None:
    assert _verdict(_posting(UNPAID), min_salary=100_000, include_unknown_salary=True).kept


def test_another_currency_is_unknown_not_converted() -> None:
    posting = _posting("Salary: $150,000 - $190,000 CAD annually")
    verdict = _verdict(posting, min_salary=100_000)

    assert not verdict.kept
    assert "not converted" in verdict.reasons[0]


def test_another_period_is_unknown_not_converted() -> None:
    verdict = _verdict(_posting("Pay: $80 - $95 per hour"), min_salary=100_000)

    assert not verdict.kept
    assert "per hour" in verdict.reasons[0]


def test_a_posting_never_extracted_says_so() -> None:
    verdict = _verdict(_posting(PAID, extracted=False), min_salary=100_000)

    assert verdict.reasons == ["requirements not read yet (make extract-requirements)"]


# --------------------------------------------------------------------------
# Skills
# --------------------------------------------------------------------------

SKILLED = """About us
Our platform runs on Java.

Requirements
- Python and PostgreSQL

Nice to have
- Kubernetes
"""


def test_a_required_skill_the_owner_lacks_excludes() -> None:
    verdict = _verdict(_posting(SKILLED), lacking_skills=("python",))

    assert verdict.reasons == ["requires Python"]


def test_a_preferred_skill_the_owner_lacks_does_not() -> None:
    assert _verdict(_posting(SKILLED), lacking_skills=("kubernetes",)).kept


def test_an_unclassified_mention_is_not_assumed_optional() -> None:
    verdict = _verdict(_posting(SKILLED), lacking_skills=("java",))

    assert not verdict.kept
    assert "without saying whether it is required" in verdict.reasons[0]
    assert _verdict(_posting(SKILLED), lacking_skills=("java",), include_unknown_skills=True).kept


def test_a_wanted_skill_must_be_named_somewhere() -> None:
    assert _verdict(_posting(SKILLED), wanted_skills=("kubernetes",)).kept
    assert _verdict(_posting(SKILLED), wanted_skills=("rust",)).reasons == ["does not name Rust"]


# --------------------------------------------------------------------------
# Education
# --------------------------------------------------------------------------


def test_a_required_higher_degree_excludes() -> None:
    posting = _posting("Requirements\n- Master's degree in Computer Science")

    assert _verdict(posting, max_education="bachelor").reasons == ["requires a master degree"]


def test_equivalent_experience_is_unknown_not_a_pass() -> None:
    posting = _posting("Requirements\n- Master's degree or equivalent experience")
    verdict = _verdict(posting, max_education="bachelor")

    assert not verdict.kept
    assert "equivalent experience" in verdict.reasons[0]


def test_a_preferred_higher_degree_is_kept() -> None:
    posting = _posting("Preferred qualifications\n- PhD in Statistics")

    assert _verdict(posting, max_education="bachelor").kept


def test_unstated_education_needs_the_owner_to_widen_the_filter() -> None:
    posting = _posting(UNPAID)

    assert _verdict(posting, max_education="bachelor").reasons == ["education is not stated"]
    assert _verdict(posting, max_education="bachelor", include_unknown_education=True).kept


def test_a_degree_at_or_below_the_owner_level_passes() -> None:
    posting = _posting("Requirements\n- Bachelor's degree in Engineering")

    assert _verdict(posting, max_education="master").kept


# --------------------------------------------------------------------------
# The description the feed shows
# --------------------------------------------------------------------------


def test_the_search_describes_its_requirement_filters() -> None:
    parts = SearchFilters(
        min_salary=150_000,
        lacking_skills=("java",),
        max_education="bachelor",
        include_unknown_education=True,
    ).describe()

    assert "pay reaching 150,000 USD per year" in parts
    assert "does not require java" in parts
    assert "education at most bachelor, or unstated" in parts


def test_an_unstated_period_is_read_as_annual_only_when_the_owner_opts_in() -> None:
    posting = _posting("Salary range: $182,800 - $247,300 USD")

    assert not _verdict(posting, min_salary=200_000).kept
    assert _verdict(posting, min_salary=200_000, salary_unstated_period_as_year=True).kept
    below = _verdict(posting, min_salary=300_000, salary_unstated_period_as_year=True)
    assert "read as annual" in below.reasons[0]
