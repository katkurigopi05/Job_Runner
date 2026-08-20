"""The score, broken into dimensions a person can argue with.

Told a posting scored 0.61, the owner cannot tell whether the skills matched
and the level was wrong, or the reverse — and so cannot tell whether to apply.
"""

from __future__ import annotations

import uuid

from packages.core.models import Posting, Profile
from packages.matching.rubric import NEUTRAL, evaluate
from packages.matching.score import ScoredPosting


def _posting(title: str = "Senior Backend Engineer", location: str = "Remote - US") -> Posting:
    return Posting(
        id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        title=title,
        location=location,
        description_raw="Build services.",
    )


def _profile() -> Profile:
    return Profile(
        id=uuid.uuid4(), candidate_id=uuid.uuid4(), label="default", location="Remote - US"
    )


def _scored(**kwargs: object) -> ScoredPosting:
    defaults: dict[str, object] = {
        "posting_id": "x",
        "score": 0.5,
        "title_similarity": 0.4,
        "body_similarity": 0.5,
        "matched_terms": ["python", "postgres"],
        "missing_terms": ["kubernetes"],
    }
    defaults.update(kwargs)
    return ScoredPosting(**defaults)  # type: ignore[arg-type]


def test_the_weakest_dimension_is_named() -> None:
    """The one worth reading first."""
    rubric = evaluate(
        _posting(),
        _profile(),
        _scored(matched_terms=[], missing_terms=["kubernetes", "terraform", "go"]),
        target_seniority="senior",
    )

    assert rubric.weakest is not None
    assert rubric.weakest.name == "skills_coverage"


def test_full_coverage_scores_higher_than_none() -> None:
    strong = evaluate(
        _posting(),
        _profile(),
        _scored(matched_terms=["a", "b", "c"], missing_terms=[]),
        target_seniority="senior",
    )
    weak = evaluate(
        _posting(),
        _profile(),
        _scored(matched_terms=[], missing_terms=["a", "b", "c"]),
        target_seniority="senior",
    )

    assert strong.overall > weak.overall


def test_an_unknown_signal_is_neutral_and_unweighted() -> None:
    """3 means "no signal", not "average". A posting that does not state its
    level is not a poor level match; rounding unknowns down would penalise
    terse postings for being terse."""
    rubric = evaluate(_posting(title="Engineer"), _profile(), _scored(), target_seniority=None)

    seniority = next(d for d in rubric.dimensions if d.name == "seniority")
    assert seniority.score == NEUTRAL
    assert seniority.weight == 0.0


def test_an_exact_level_beats_one_rung_off() -> None:
    exact = evaluate(
        _posting(title="Senior Backend Engineer"), _profile(), _scored(), target_seniority="senior"
    )
    # "staff" groups with "senior" in filters.SENIORITY_LEVELS, so principal
    # is the nearest genuinely different rung.
    off = evaluate(
        _posting(title="Principal Backend Engineer"),
        _profile(),
        _scored(),
        target_seniority="senior",
    )

    exact_dim = next(d for d in exact.dimensions if d.name == "seniority")
    off_dim = next(d for d in off.dimensions if d.name == "seniority")
    assert exact_dim.score > off_dim.score


def test_an_excluded_posting_says_why() -> None:
    """The case where the owner most wants a reason — a bare 0.0 reads as a
    bad match rather than a filtered one."""
    rubric = evaluate(
        _posting(),
        _profile(),
        _scored(score=0.0, excluded_by=["posting states it cannot sponsor"]),
        target_seniority="senior",
    )

    eligibility = next(d for d in rubric.dimensions if d.name == "eligibility")
    assert eligibility.score == 1.0
    assert "sponsor" in eligibility.finding


def test_the_finding_names_the_missing_terms() -> None:
    rubric = evaluate(
        _posting(),
        _profile(),
        _scored(missing_terms=["kubernetes", "terraform"]),
        target_seniority="senior",
    )

    coverage = next(d for d in rubric.dimensions if d.name == "skills_coverage")
    assert "kubernetes" in coverage.finding


def test_a_posting_with_no_comparable_terms_is_unweighted() -> None:
    rubric = evaluate(
        _posting(),
        _profile(),
        _scored(matched_terms=[], missing_terms=[]),
        target_seniority="senior",
    )

    coverage = next(d for d in rubric.dimensions if d.name == "skills_coverage")
    assert coverage.weight == 0.0
    assert coverage.score == NEUTRAL


def test_the_overall_stays_on_the_stated_scale() -> None:
    for matched, missing in (([], ["a"] * 10), (["a"] * 10, []), (["a"], ["b"])):
        rubric = evaluate(
            _posting(),
            _profile(),
            _scored(matched_terms=matched, missing_terms=missing),
            target_seniority="senior",
        )
        assert 1.0 <= rubric.overall <= 5.0


def test_the_rubric_does_not_touch_the_ranking_score() -> None:
    """It explains the ranking; it is not evidence independent of it. Ranking
    by it would look like a second opinion while being the same one."""
    scored = _scored(score=0.42)

    evaluate(_posting(), _profile(), scored, target_seniority="senior")

    assert scored.score == 0.42


def test_the_rubric_reaches_the_match_feed() -> None:
    from packages.matching.embed import LexicalEmbedder
    from packages.matching.score import score_posting

    embedder = LexicalEmbedder()
    text = "Senior backend engineer. Python and PostgreSQL."
    result = score_posting(
        _posting(),
        _profile(),
        embedder.encode([text])[0],
        embedder,
        profile_text_value=text,
        target_seniority="senior",
    )

    assert result.reasons()["rubric"]
    assert result.rubric["weakest"] is not None or result.rubric["overall"]
