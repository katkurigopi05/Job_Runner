"""Posting legitimacy, and the liveness re-check.

Both answer "is this real and open", which the match score cannot and should
not. The tests that matter are the ones asserting what these refuse to
conclude — a warning that fires on ordinary postings stops being read.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from packages.core.models import Posting
from packages.crawler.liveness import State, read_response
from packages.matching.legitimacy import Tier, Weight, assess, specificity

NOW = datetime(2026, 8, 19, tzinfo=UTC)

GOOD_DESCRIPTION = """
We are hiring a Senior Backend Engineer for the Payments team at Acme. You will
own the billing service, which processes about 40 million transactions a month
across Postgres and Kafka. The team is six engineers and reports to the Director
of Payments. You will work in Python and Go, deploy on Kubernetes, and share the
on-call rotation. Recent projects include a ledger rewrite and a migration from
Redis to Postgres for idempotency keys. We use Terraform for infrastructure and
Datadog for observability. Interviews are a screen, a system design round, and a
take-home reviewed by the team you would join.
"""

BOILERPLATE_DESCRIPTION = """
We are looking for a talented and motivated individual to join our fast-paced
and dynamic team. The ideal candidate will be a self-starter with excellent
communication skills and a passion for excellence. You will work closely with
cross-functional stakeholders to drive impact and deliver value. This is an
exciting opportunity to grow your career in a collaborative environment where
your contributions matter. We offer competitive compensation and great benefits.
If you are passionate about making a difference, we want to hear from you.
"""


def _posting(
    *,
    description: str = GOOD_DESCRIPTION,
    title: str = "Senior Backend Engineer",
    days_old: int = 3,
    location: str | None = "Remote - US",
    company_id: uuid.UUID | None = None,
) -> Posting:
    return Posting(
        id=uuid.uuid4(),
        company_id=company_id or uuid.uuid4(),
        title=title,
        location=location,
        description_raw=description,
        first_seen_at=NOW - timedelta(days=days_old),
    )


# --------------------------------------------------------------------------
# Tiers
# --------------------------------------------------------------------------


def test_a_fresh_specific_posting_is_high_confidence() -> None:
    result = assess(_posting(), now=NOW)

    assert result.tier is Tier.HIGH_CONFIDENCE
    assert not result.concerning


REPETITIVE_DESCRIPTION = " ".join(
    ["We are a collaborative fast-paced dynamic exciting collaborative team."] * 20
)


def test_thin_content_with_age_is_suspicious() -> None:
    """Two independent concerns, which is the bar."""
    result = assess(_posting(description=BOILERPLATE_DESCRIPTION, days_old=200), now=NOW)

    assert result.tier is Tier.SUSPICIOUS
    assert len(result.concerning) >= 2


def test_repetition_does_not_count_as_substance() -> None:
    """Padding a description to length by saying one thing twenty times."""
    result = assess(_posting(description=REPETITIVE_DESCRIPTION), now=NOW)

    quality = next(s for s in result.signals if s.name == "description_quality")
    assert quality.weight is Weight.CONCERNING
    assert "boilerplate" in quality.finding


def test_the_specificity_threshold_is_not_tuned_to_these_fixtures() -> None:
    """Both hand-written fixtures sit above the line on purpose.

    A threshold placed between two samples this repo authored would report a
    number that only measures the samples — docs/REFERENCE.md §3.6.
    """
    from packages.matching.legitimacy import MIN_SPECIFICITY

    assert specificity(GOOD_DESCRIPTION) > MIN_SPECIFICITY
    assert specificity(BOILERPLATE_DESCRIPTION) > MIN_SPECIFICITY


def test_one_concern_alone_is_only_caution() -> None:
    """An old posting is common and innocent — executive and government
    hiring runs long. Calling it suspicious makes the tier noise."""
    result = assess(_posting(days_old=200), now=NOW)

    assert result.tier is Tier.CAUTION


def test_a_short_description_cannot_state_a_scope() -> None:
    result = assess(_posting(description="Great role. Apply now!"), now=NOW)

    quality = next(s for s in result.signals if s.name == "description_quality")
    assert quality.weight is Weight.CONCERNING


def test_repeated_postings_for_one_title_are_flagged() -> None:
    """The classic ghost tell: the same role reappearing under new ids."""
    company = uuid.uuid4()
    target = _posting(company_id=company)
    siblings = [target, _posting(company_id=company), _posting(company_id=company)]

    result = assess(target, siblings=siblings, now=NOW)

    reposting = next(s for s in result.signals if s.name == "reposting")
    assert reposting.weight is Weight.CONCERNING


def test_the_same_title_at_a_different_company_is_not_reposting() -> None:
    target = _posting(company_id=uuid.uuid4())
    other = _posting(company_id=uuid.uuid4())

    result = assess(target, siblings=[target, other], now=NOW)

    reposting = next(s for s in result.signals if s.name == "reposting")
    assert reposting.weight is Weight.POSITIVE


def test_specificity_separates_a_real_posting_from_filler() -> None:
    assert specificity(GOOD_DESCRIPTION) > specificity(BOILERPLATE_DESCRIPTION)


# --------------------------------------------------------------------------
# Advisories — true of real postings too, so never part of the tier
# --------------------------------------------------------------------------


def test_contract_wording_is_an_advisory_not_a_tier_change() -> None:
    """A contract role is a real job. It is still worth knowing before you
    apply believing it is employment."""
    posting = _posting(description=GOOD_DESCRIPTION + "\nThis is a 1099 engagement.")

    result = assess(posting, now=NOW)

    assert result.tier is Tier.HIGH_CONFIDENCE
    assert any(s.name == "employment_classification" for s in result.advisories)


def test_benefits_from_the_wrong_country_are_flagged() -> None:
    posting = _posting(
        location="Toronto, Canada",
        description=GOOD_DESCRIPTION + "\nWe offer a 401(k) with company match.",
    )

    result = assess(posting, now=NOW)

    assert any(s.name == "benefits_geography" for s in result.advisories)


def test_a_placeholder_salary_range_is_flagged() -> None:
    posting = _posting(description=GOOD_DESCRIPTION + "\nCompensation: $60,000 - $400,000.")

    result = assess(posting, now=NOW)

    assert any(s.name == "salary_range" for s in result.advisories)


def test_a_normal_salary_range_is_not_flagged() -> None:
    posting = _posting(description=GOOD_DESCRIPTION + "\nCompensation: $160,000 - $200,000.")

    result = assess(posting, now=NOW)

    assert not any(s.name == "salary_range" for s in result.advisories)


def test_legitimacy_never_becomes_a_number() -> None:
    """The whole point of keeping it separate. A ghost job written well would
    otherwise rank highly *because* it is written well."""
    result = assess(_posting(), now=NOW)

    assert isinstance(result.as_dict()["tier"], str)
    assert "score" not in result.as_dict()


# --------------------------------------------------------------------------
# Liveness
# --------------------------------------------------------------------------


def test_a_404_means_the_posting_is_gone() -> None:
    assert read_response(404, "").state is State.CLOSED
    assert read_response(410, "").state is State.CLOSED


def test_a_closure_phrase_means_gone() -> None:
    assert read_response(200, "This job is no longer accepting applications.").is_closed


def test_a_rate_limit_is_not_a_closure() -> None:
    """A site refusing us is not a job that ended."""
    assert read_response(429, "slow down").state is State.UNKNOWN
    assert read_response(503, "").state is State.UNKNOWN


def test_a_server_error_is_not_a_closure() -> None:
    assert read_response(500, "oops").state is State.UNKNOWN


def test_an_empty_body_is_not_a_closure() -> None:
    assert read_response(200, "   ").state is State.UNKNOWN


def test_a_normal_page_is_open() -> None:
    assert read_response(200, "<h1>Senior Backend Engineer</h1> Apply now").state is State.OPEN
