"""The one inbound message with a clock on it.

Taken from a job-tracker spec whose status pipeline carried
`ONLINE_ASSIGNMENT` as a stage of its own. Ours did not, and the three
commonest phrasings landed badly:

    "Complete your online assessment ... within 5 days"  -> info_request
    "Coding challenge ... Codility link. 72 hours."      -> abstained
    "Take-home exercise ... return within a week"        -> abstained

`info_request` reads as paperwork and an abstention resolves to `noise`, so
either way the window expires while the tracker looks calm. That is a worse
failure than a missed rejection: a rejection is already over, an assessment is
an opportunity with a deadline.
"""

from __future__ import annotations

import pytest

from packages.core.enums import OUTCOME_FOR_CLASSIFICATION, Classification, Outcome
from packages.inbox.classify import RuleClassifier


def _classify(subject: str, body: str) -> Classification | None:
    verdict = RuleClassifier().classify(subject, body)
    return None if verdict.abstained else verdict.classification


@pytest.mark.parametrize(
    ("subject", "body"),
    [
        ("Complete your online assessment", "Please finish it within 5 days to move forward."),
        ("Coding challenge", "Here is your Codility link. You have 72 hours."),
        ("Take-home exercise", "Attached is a take-home assignment; return it within a week."),
        ("Next step", "Please complete the assessment linked below."),
        ("Technical assessment invitation", "Your CodeSignal test is ready."),
        ("Skills test", "We use HackerRank for the first round."),
        ("Your assessment link", "Follow the assessment link to begin."),
    ],
)
def test_an_assessment_invite_is_its_own_class(subject: str, body: str) -> None:
    assert _classify(subject, body) is Classification.ASSESSMENT


@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        (
            "Interview invitation",
            "We would like to invite you to a technical interview next week.",
            Classification.INTERVIEW,
        ),
        (
            "Rejection",
            "We have decided to move forward with another candidate.",
            Classification.REJECTION,
        ),
        ("Offer", "We are pleased to offer you the position.", Classification.OFFER),
    ],
)
def test_the_rule_does_not_swallow_its_neighbours(
    subject: str, body: str, expected: Classification
) -> None:
    """It sits before INTERVIEW because an assessment invite borrows the same
    vocabulary — "next step", "move forward". A rule placed there and written
    loosely would capture the interview and the offer with it."""
    assert _classify(subject, body) is expected


def test_assessing_an_application_is_not_an_assessment() -> None:
    """Every alternative names the artefact or a platform, never a bare
    "assessment": "we will assess your application" is not a coding test."""
    assert _classify("Update", "We will assess your application and be in touch.") is None


def test_it_records_an_outcome_between_info_and_interview() -> None:
    """An assessment is a real advance on being asked for paperwork, and it
    almost always precedes an interview rather than replacing one."""
    from packages.inbox.route import _OUTCOME_RANK

    assert OUTCOME_FOR_CLASSIFICATION[Classification.ASSESSMENT] is Outcome.ASSESSMENT
    assert (
        _OUTCOME_RANK["info_requested"] < _OUTCOME_RANK["assessment"] < _OUTCOME_RANK["interview"]
    )


def test_an_interview_still_outranks_an_assessment() -> None:
    """The ranking is what stops a later, weaker message walking the outcome
    backwards — an assessment reminder arriving after the interview is booked
    must not reset the record."""
    from packages.inbox.route import _OUTCOME_RANK

    assert _OUTCOME_RANK["assessment"] < _OUTCOME_RANK["interview"] < _OUTCOME_RANK["offer"]
    assert _OUTCOME_RANK["rejected"] == _OUTCOME_RANK["offer"]


def test_the_labeled_thirty_are_unchanged() -> None:
    """Adding a class before INTERVIEW is where precision goes to die."""
    from tests.test_inbox import LABELED

    classifier = RuleClassifier()
    wrong = [
        (want, classifier.classify(subject, body))
        for want, subject, body in LABELED
        if not classifier.classify(subject, body).abstained
        and classifier.classify(subject, body).classification is not want
    ]

    assert not wrong, wrong
