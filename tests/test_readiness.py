"""The composite in front of every approval, and the gate beside it.

The tests that matter here are the ones about *disagreement* between the score
and the gate. A composite that simply averaged four numbers would pass most of
this file; what it would fail is every test asserting that a fact about
completability outranks an opinion about quality.
"""

from __future__ import annotations

import pytest

from packages.tailor.readiness import (
    MIN_COMPONENTS,
    READY_FLOOR,
    Component,
    Readiness,
    Tier,
    assess,
)


def _strong(**overrides: object) -> Readiness:
    """An application with every axis measured and scoring well."""
    kwargs: dict[str, object] = {
        "rubric_overall": 4.8,
        "ats_parse": 0.95,
        "ats_keywords": 0.85,
        "recruiter_overall": 0.9,
        "recruiter_shortlist": "strong yes",
        "fill_rate": 1.0,
    }
    kwargs.update(overrides)
    return assess(**kwargs)  # type: ignore[arg-type]


# --- the gate is not the score ------------------------------------------------


def test_a_blocker_beats_a_perfect_score() -> None:
    """§53's gate is a fact about completability, not a threshold on quality.

    This is the test the module exists for. Every measurable axis is perfect
    and one required question has no answer, which is exactly the case where
    an averaged composite would wave it through.
    """
    ready = _strong(unanswered_required=["Are you legally authorized to work?"])

    assert ready.score is not None and ready.score > 0.9, "the quality opinion is still high"
    assert ready.tier in {Tier.EXCEPTIONAL, Tier.VERY_STRONG}
    assert ready.is_ready is False, "and it is still not sendable"


def test_the_blocker_names_the_question_not_the_category() -> None:
    question = "Do you now or will you in the future require sponsorship?"
    ready = _strong(unanswered_required=[question])

    assert question in ready.blockers[0].detail
    assert question in ready.summary()


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"missing_profile_fields": ["profile.phone"]}, "incomplete_profile"),
        ({"has_resume": False}, "no_resume"),
        ({"unanswered_required": ["Why us?"]}, "unanswered_required"),
        ({"knock_outs": ["Do you have 10 years of Rust?"]}, "knock_out"),
        ({"excluded_by": ["location: Remote - EMEA"]}, "hard_filter"),
    ],
)
def test_every_blocker_kind_stops_ready(kwargs: dict[str, object], code: str) -> None:
    """Each of the five is independently disqualifying.

    Parametrised rather than merged into one call so a regression names which
    kind stopped working, instead of reporting that "a blocker" broke.
    """
    ready = _strong(**kwargs)

    assert ready.is_ready is False
    assert [b.code for b in ready.blockers] == [code]


def test_a_clean_strong_application_is_ready() -> None:
    ready = _strong()

    assert ready.blockers == []
    assert ready.is_ready is True
    assert ready.score is not None and ready.score >= READY_FLOOR


# --- an unmeasured component is absent, not zero ------------------------------


def test_an_unmeasured_component_takes_no_weight() -> None:
    """A missing posting must not drag the score down as if it scored 0."""
    with_posting = _strong()
    without = _strong(ats_keywords=None)

    assert without.score is not None and with_posting.score is not None
    assert without.score > 0.8, "not dragged toward zero by the absent half"


def test_an_unmeasured_component_still_appears_with_its_reason() -> None:
    """A component that vanished when its input did would read as one that passed."""
    ready = _strong(recruiter_overall=None, recruiter_shortlist=None)

    recruiter = next(c for c in ready.components if c.name == "recruiter")
    assert recruiter.measured is False
    assert recruiter.weight == 0.0
    assert "not read as a recruiter would" in recruiter.finding
    assert recruiter.name in [c["name"] for c in ready.as_dict()["components"]]  # type: ignore[index]


def test_the_ats_component_survives_a_missing_posting() -> None:
    """An unparseable résumé is unparseable whatever job it is aimed at."""
    ready = _strong(ats_keywords=None)

    ats = next(c for c in ready.components if c.name == "ats")
    assert ats.measured is True
    assert ats.score == pytest.approx(0.95)
    assert "no posting" in ats.finding


# --- below three components, no score at all ----------------------------------


def test_two_components_report_no_score_rather_than_a_rename() -> None:
    """docs/BACKLOG.md names this trap by name: two inputs is a rename."""
    ready = assess(ats_parse=0.9, fill_rate=1.0)

    assert len(ready.measured) < MIN_COMPONENTS
    assert ready.score is None
    assert ready.tier is None
    assert ready.is_ready is False, "unassessed is not ready"
    assert "not assessed" in ready.summary()


def test_exactly_three_components_is_enough() -> None:
    ready = assess(rubric_overall=4.5, ats_parse=0.9, fill_rate=1.0)

    assert len(ready.measured) == MIN_COMPONENTS
    assert ready.score is not None
    assert ready.tier is not None


def test_unassessed_is_not_the_same_answer_as_weak() -> None:
    """Both are "not ready"; only one of them is a judgement about the résumé."""
    unassessed = assess(ats_parse=0.9, fill_rate=1.0)
    weak = _strong(
        rubric_overall=1.0, ats_parse=0.2, ats_keywords=0.1, recruiter_overall=0.1, fill_rate=0.2
    )

    assert unassessed.score is None and unassessed.tier is None
    assert weak.score is not None and weak.tier is Tier.WEAK
    assert unassessed.is_ready is weak.is_ready is False


# --- the bands ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.0, Tier.EXCEPTIONAL),
        (0.95, Tier.EXCEPTIONAL),
        (0.94, Tier.VERY_STRONG),
        (0.90, Tier.VERY_STRONG),
        (0.89, Tier.STRONG),
        (0.80, Tier.STRONG),
        (0.79, Tier.MODERATE),
        (0.65, Tier.MODERATE),
        (0.64, Tier.WEAK),
        (0.0, Tier.WEAK),
    ],
)
def test_the_bands_are_contiguous_and_inclusive_at_the_floor(value: float, expected: Tier) -> None:
    """Every boundary is tested from both sides, so a shifted band is visible.

    A gap between two bands would make `tier` raise on the value in the gap —
    `next()` over `_BANDS` has no default, deliberately.
    """
    ready = Readiness(
        components=[Component(name, value, 1.0, "fixture") for name in ("a", "b", "c")]
    )
    assert ready.score == pytest.approx(value)
    assert ready.tier is expected


# --- the normalisation that would otherwise flatter ---------------------------


def test_an_excluded_posting_normalises_to_zero_not_a_fifth() -> None:
    """The rubric's floor is 1.0 and means excluded, so `/5` would report 20%."""
    ready = _strong(rubric_overall=1.0)

    match = next(c for c in ready.components if c.name == "match")
    assert match.score == pytest.approx(0.0)


def test_a_perfect_rubric_normalises_to_one() -> None:
    ready = _strong(rubric_overall=5.0)

    match = next(c for c in ready.components if c.name == "match")
    assert match.score == pytest.approx(1.0)


# --- the serialised shape the review screen reads -----------------------------


def test_the_dict_carries_the_gate_and_the_reasons() -> None:
    ready = _strong(knock_outs=["Do you have an active TS/SCI clearance?"])
    payload = ready.as_dict()

    assert payload["ready"] is False
    assert payload["blockers"] == [
        {
            "code": "knock_out",
            "detail": "likely disqualifying: Do you have an active TS/SCI clearance?",
        }
    ]
    assert payload["tier"] in {t.value for t in Tier}
    assert payload["weakest"] in {"match", "ats", "recruiter", "form"}


def test_the_weakest_component_ignores_unmeasured_ones() -> None:
    """An unmeasured component scores 0.0 and would otherwise always win."""
    ready = _strong(recruiter_overall=None, ats_parse=0.4, ats_keywords=0.4)

    assert ready.weakest is not None
    assert ready.weakest.name == "ats"
