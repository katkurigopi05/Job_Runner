"""Advertised pay against what the profile asked for.

`Profile.salary_expectation` was held and only ever typed onto a form. The
tests that matter are the refusals: this reports, it never answers, and it
never guesses at a number the owner did not give.
"""

from __future__ import annotations

import uuid

import pytest

from packages.core.models import Posting, Profile
from packages.matching.rubric import evaluate
from packages.matching.salary import Comparison, compare, parse_range
from packages.matching.score import ScoredPosting


def _posting(text: str) -> Posting:
    return Posting(
        id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        title="Backend Engineer",
        url="https://boards.greenhouse.io/acme/jobs/1",
        description_raw=text,
    )


def _profile(expectation: str | None) -> Profile:
    return Profile(
        id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        label="default",
        salary_expectation=expectation,
    )


# --------------------------------------------------------------------------
# Reading a figure
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "low", "high"),
    [
        ("$120,000 - $160,000", 120_000, 160_000),
        ("$120k-$160k", 120_000, 160_000),
        ("Salary: 120,000 to 160,000", 120_000, 160_000),
        ("up to $160k", 160_000, 160_000),
    ],
)
def test_a_range_is_read(text: str, low: float, high: float) -> None:
    parsed = parse_range(text)

    assert parsed is not None
    assert (parsed.low, parsed.high) == (low, high)


def test_a_single_figure_is_a_zero_width_range() -> None:
    """ "up to $160k" and "$160k-$160k" mean the same for a comparison, and
    inventing a spread would read something into it the posting did not say."""
    parsed = parse_range("$160,000 per year")

    assert parsed is not None
    assert parsed.low == parsed.high == 160_000


def test_an_hourly_rate_is_not_an_annual_salary() -> None:
    assert parse_range("$45 - $60 per hour") is None


def test_prose_with_no_figure_reads_as_nothing() -> None:
    assert parse_range("We offer competitive compensation and great benefits.") is None


def test_a_reversed_range_is_normalised() -> None:
    parsed = parse_range("$160,000 - $120,000")

    assert parsed is not None
    assert parsed.low < parsed.high


# --------------------------------------------------------------------------
# Comparing, and declining to
# --------------------------------------------------------------------------


def test_an_overlapping_range_is_within() -> None:
    assert compare("$120,000 - $160,000", "$150,000").comparison is Comparison.WITHIN


def test_a_lower_range_is_below_and_says_by_how_much() -> None:
    result = compare("$80,000 - $95,000", "$150,000")

    assert result.comparison is Comparison.BELOW
    assert "%" in result.finding


def test_a_higher_range_is_above() -> None:
    assert compare("$200k - $260k", "$150,000 - $180,000").comparison is Comparison.ABOVE


def test_a_posting_that_says_nothing_is_unknown_not_penalised() -> None:
    """Some states now require a range, so silence says nothing about the
    employer — and scoring it as a negative would penalise saying less."""
    assert compare("Great benefits.", "$150,000").comparison is Comparison.UNKNOWN


def test_a_vague_expectation_is_not_guessed_at() -> None:
    """ "competitive" is not a number, and reading one out of it is inventing
    a figure the owner never gave."""
    for vague in ("competitive", "negotiable", "DOE", ""):
        assert compare("$120,000 - $160,000", vague).comparison is Comparison.UNKNOWN


def test_a_different_currency_is_not_converted() -> None:
    """A rate the owner never chose would turn an assumption into a confident
    comparison, and a wrong one makes someone skip a job they wanted."""
    result = compare("£90,000 - £110,000", "$150,000")

    assert result.comparison is Comparison.UNKNOWN
    assert "not converted" in result.finding


def test_a_missing_expectation_is_unknown() -> None:
    assert compare("$120,000 - $160,000", None).comparison is Comparison.UNKNOWN


# --------------------------------------------------------------------------
# In the rubric
# --------------------------------------------------------------------------


def _scored() -> ScoredPosting:
    # A decent title match and full coverage, so the salary dimension is the
    # only thing that can be weakest — otherwise the test proves nothing.
    return ScoredPosting(
        posting_id="x",
        score=0.5,
        title_similarity=0.5,
        matched_terms=["python", "postgres"],
        missing_terms=[],
    )


def test_an_unknown_salary_carries_no_weight() -> None:
    rubric = evaluate(_posting("Great benefits."), _profile("$150,000"), _scored())

    salary = next(d for d in rubric.dimensions if d.name == "salary")
    assert salary.weight == 0.0


def test_a_low_offer_drags_the_rubric_down() -> None:
    good = evaluate(_posting("$150,000 - $190,000"), _profile("$150,000"), _scored())
    poor = evaluate(_posting("$70,000 - $80,000"), _profile("$150,000"), _scored())

    assert poor.overall < good.overall
    assert poor.weakest is not None and poor.weakest.name == "salary"


def test_the_comparison_reaches_the_feed() -> None:
    from packages.matching.embed import LexicalEmbedder
    from packages.matching.score import score_posting

    embedder = LexicalEmbedder()
    text = "Backend engineer, Python."
    result = score_posting(
        _posting("Compensation: $80,000 - $95,000."),
        _profile("$150,000"),
        embedder.encode([text])[0],
        embedder,
        profile_text_value=text,
    )

    assert result.reasons()["salary"]
    assert result.salary["comparison"] == "below"


def test_salary_never_becomes_a_form_answer() -> None:
    """§2.2: the profile's figure is copied verbatim onto a form and this
    module has no part in that. It reads; it does not answer."""
    from packages.ats.answers import VERBATIM_KEYS

    assert "salary_expectation" in VERBATIM_KEYS
