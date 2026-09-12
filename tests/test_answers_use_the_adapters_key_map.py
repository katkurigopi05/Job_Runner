"""The adapters knew the field names. The answer builder never asked.

`profile_key_for` is defined in lever.py, ashby.py and workable.py — each one
stating, authoritatively, that `_systemfield_name` is the full name and
`urls[LinkedIn]` is the LinkedIn URL. None of the three had a caller outside
its own test file. `build_answers` matched on a regex over the label instead,
so an ATS whose labels are terse or whose questions are named by uuid filled
almost nothing.

Measured on a live ElevenLabs posting before the fix: **1 of 9 fields**. Gate 2
asks for 80%. Nothing failed — the unfilled fields went to `needs_review`
carrying their exact wording, which is §2.4 working — but the owner was being
handed a form to finish by hand that the profile could have answered.
"""

from __future__ import annotations

import uuid

import pytest

from packages.ats.answers import build_answers
from packages.ats.base import Question, QuestionKind
from packages.core.models import Candidate, Profile

CANDIDATE = Candidate(
    id=uuid.uuid4(), user_id=uuid.uuid4(), name="Ada Lovelace", email="ada@example.com"
)
PROFILE = Profile(
    id=uuid.uuid4(),
    candidate_id=CANDIDATE.id,
    label="fixture",
    phone="+1-555-0100",
    location="Austin, TX",
    work_auth="US citizen",
    needs_sponsorship=False,
    links_json={"linkedin": "https://linkedin.com/in/ada", "github": "https://github.com/ada"},
    answers_kv_json={},
)


def _q(key: str, label: str, kind: QuestionKind = QuestionKind.TEXT) -> Question:
    return Question(key=key, label=label, kind=kind, required=True)


def test_a_terse_label_still_finds_the_name() -> None:
    """Ashby labels its name field `name`, and that used to match nothing.

    `_match_attribute` searched `"{label} {key}"` as one string, so the `^name$`
    rule could never fire: the haystack was always longer than the label.
    """
    questions = [_q("_systemfield_name", "name")]

    answers = build_answers(questions, CANDIDATE, PROFILE, ats="ashby")

    assert answers["_systemfield_name"] == "Ada Lovelace"


@pytest.mark.parametrize(
    ("ats", "key", "expected"),
    [
        ("ashby", "_systemfield_name", "Ada Lovelace"),
        ("ashby", "_systemfield_email", "ada@example.com"),
        ("ashby", "_systemfield_phone", "+1-555-0100"),
        ("ashby", "_systemfield_linkedin", "https://linkedin.com/in/ada"),
        ("lever", "name", "Ada Lovelace"),
        ("lever", "email", "ada@example.com"),
        ("lever", "urls[LinkedIn]", "https://linkedin.com/in/ada"),
        ("lever", "urls[GitHub]", "https://github.com/ada"),
        ("workable", "firstname", "Ada"),
        ("workable", "lastname", "Lovelace"),
        ("workable", "email", "ada@example.com"),
    ],
)
def test_the_adapters_own_field_names_are_answered(ats, key, expected) -> None:
    """The ATS named the field. That is better evidence than a regex."""
    answers = build_answers([_q(key, "")], CANDIDATE, PROFILE, ats=ats)

    assert answers[key] == expected


def test_an_unrecognised_field_is_still_left_unanswered() -> None:
    """§2.4 — a `cards[<uuid>]` question is not something to guess at."""
    questions = [_q("cards[e6a8b66e][auth]", "Tell us about a hard problem")]

    answers = build_answers(questions, CANDIDATE, PROFILE, ats="lever")

    assert "cards[e6a8b66e][auth]" not in answers


def test_the_key_map_does_not_override_an_owners_own_answer() -> None:
    """An answer supplied at review wins over anything derived."""
    questions = [_q("_systemfield_name", "name")]

    answers = build_answers(
        questions, CANDIDATE, PROFILE, ats="ashby", extra={"_systemfield_name": "A. Lovelace"}
    )

    assert answers["_systemfield_name"] == "A. Lovelace"


def test_a_resume_key_still_needs_a_file_to_point_at() -> None:
    """The map says `resume`; without a path there is nothing to attach."""
    questions = [_q("_systemfield_resume", "resume", QuestionKind.FILE)]

    assert "_systemfield_resume" not in build_answers(questions, CANDIDATE, PROFILE, ats="ashby")

    answered = build_answers(questions, CANDIDATE, PROFILE, ats="ashby", resume_path="/tmp/a.pdf")
    assert answered["_systemfield_resume"] == "/tmp/a.pdf"


def test_an_unknown_ats_falls_back_to_the_label_rules() -> None:
    """A name this registry does not know must not raise mid-apply."""
    answers = build_answers([_q("x", "Email")], CANDIDATE, PROFILE, ats="nonesuch")

    assert answers["x"] == "ada@example.com"


def test_omitting_the_ats_behaves_as_it_always_did() -> None:
    answers = build_answers([_q("first_name", "First Name")], CANDIDATE, PROFILE)

    assert answers["first_name"] == "Ada"
