"""Search filters — what the owner asked to see, not a reading of their profile."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from packages.core.models import Posting
from packages.matching.search import (
    SearchFilters,
    detect_seniority,
    is_remote,
    matches,
)


def _posting(**kwargs) -> Posting:
    defaults = dict(
        id=uuid.uuid4(),
        url="https://boards.greenhouse.io/acme/jobs/1",
        title="Senior Backend Engineer",
        location="Remote — US",
        description_raw="Python and PostgreSQL.",
        first_seen_at=datetime.now(UTC),
        closed_at=None,
    )
    return Posting(**{**defaults, **kwargs})


def test_an_empty_filter_keeps_everything() -> None:
    assert matches(_posting(), SearchFilters()).kept
    assert SearchFilters().is_empty


def test_keywords_must_all_appear() -> None:
    posting = _posting(description_raw="We use Python and Postgres.")

    assert matches(posting, SearchFilters(keywords=("python",))).kept
    verdict = matches(posting, SearchFilters(keywords=("python", "kubernetes")))
    assert not verdict.kept
    assert "missing keyword 'kubernetes'" in verdict.reasons


def test_every_reason_is_reported_not_just_the_first() -> None:
    """Showing one reason invites fixing it and being surprised nothing changed."""
    posting = _posting(title="Junior Analyst", location="Berlin", description_raw="SQL.")

    verdict = matches(
        posting,
        SearchFilters(keywords=("python",), locations=("Austin",), min_seniority="senior"),
    )

    assert not verdict.kept
    assert len(verdict.reasons) == 3


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Senior Staff Engineer", "staff"),
        ("Sr. Backend Engineer", "senior"),
        ("Principal Architect", "principal"),
        ("Software Engineering Intern", "intern"),
        ("New Grad Engineer", "junior"),
        ("Backend Engineer", None),
    ],
)
def test_seniority_detection(title: str, expected: str | None) -> None:
    """'Senior Staff' is staff. Checking `senior` first would file it a rung low."""
    assert detect_seniority(title) == expected


def test_unreadable_seniority_is_kept() -> None:
    """A title that does not state a level is not a reason to hide the job."""
    posting = _posting(title="Backend Engineer", description_raw="")

    assert matches(posting, SearchFilters(min_seniority="senior")).kept


def test_seniority_range() -> None:
    junior = _posting(title="Junior Engineer")
    principal = _posting(title="Principal Engineer")

    assert not matches(junior, SearchFilters(min_seniority="senior")).kept
    assert not matches(principal, SearchFilters(max_seniority="senior")).kept
    assert matches(_posting(title="Senior Engineer"), SearchFilters(min_seniority="senior")).kept


def test_remote_detection_prefers_the_location_over_the_body() -> None:
    """A body mentioning remote is weaker evidence than a location that says so."""
    assert is_remote(_posting(location="Remote — US"))
    assert not is_remote(
        _posting(location="Austin, TX (Hybrid)", description_raw="Some remote work possible.")
    )


def test_remote_filter_both_directions() -> None:
    remote = _posting(location="Remote — US")
    onsite = _posting(location="Austin, TX", description_raw="On-site role.")

    assert matches(remote, SearchFilters(remote=True)).kept
    assert not matches(onsite, SearchFilters(remote=True)).kept
    assert matches(onsite, SearchFilters(remote=False)).kept


def test_recency() -> None:
    old = _posting(first_seen_at=datetime.now(UTC) - timedelta(days=40))

    assert not matches(old, SearchFilters(posted_within_days=7)).kept
    assert matches(old, SearchFilters(posted_within_days=90)).kept


def test_closed_postings_are_dropped_unless_asked_for() -> None:
    closed = _posting(closed_at=datetime.now(UTC))

    assert not matches(closed, SearchFilters()).kept
    assert matches(closed, SearchFilters(include_closed=True)).kept


def test_describe_reads_as_the_owner_set_it() -> None:
    filters = SearchFilters(
        keywords=("python",), locations=("Austin",), remote=True, min_seniority="senior"
    )

    assert filters.describe() == [
        "keywords: python",
        "location: Austin",
        "remote only",
        "senior or above",
    ]
