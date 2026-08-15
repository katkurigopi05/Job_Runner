"""Question → profile-answer mapping.

The rules that matter here are refusals: when the mapper is not confident, it
must leave the field unanswered so the owner sees the employer's exact
question, rather than filling in something close enough.
"""

from __future__ import annotations

import uuid

import pytest

from packages.ats.answers import build_answers, profile_values
from packages.ats.base import Option, Question, QuestionKind
from packages.core.models import Candidate, Profile


@pytest.fixture
def candidate() -> Candidate:
    return Candidate(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="Ada Lovelace",
        email="ada@example.com",
    )


@pytest.fixture
def profile() -> Profile:
    return Profile(
        id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        label="backend",
        phone="+1-555-0100",
        location="Austin, TX",
        work_auth="US citizen",
        needs_sponsorship=False,
        salary_expectation="$180,000",
        links_json={"linkedin": "https://linkedin.com/in/ada", "github": "gh/ada"},
        answers_kv_json={},
    )


def q(key: str, label: str, kind=QuestionKind.TEXT, **kw) -> Question:
    return Question(key=key, label=label, kind=kind, **kw)


def test_name_is_split(candidate, profile) -> None:
    values = profile_values(candidate, profile)
    assert values["first_name"] == "Ada"
    assert values["last_name"] == "Lovelace"
    assert values["full_name"] == "Ada Lovelace"


def test_single_word_name_does_not_crash(profile) -> None:
    solo = Candidate(id=uuid.uuid4(), user_id=uuid.uuid4(), name="Prince", email="p@x.com")
    values = profile_values(solo, profile)
    assert values["first_name"] == "Prince"
    assert values["last_name"] == ""


def test_maps_standard_fields(candidate, profile) -> None:
    questions = [
        q("first_name", "First Name"),
        q("last_name", "Last Name"),
        q("email", "Email", QuestionKind.EMAIL),
        q("phone", "Phone", QuestionKind.PHONE),
    ]
    answers = build_answers(questions, candidate, profile)

    assert answers["first_name"] == "Ada"
    assert answers["last_name"] == "Lovelace"
    assert answers["email"] == "ada@example.com"
    assert answers["phone"] == "+1-555-0100"


def test_maps_by_label_not_just_key(candidate, profile) -> None:
    """Greenhouse custom questions have opaque keys; the label carries meaning."""
    questions = [q("job_application_answers_attributes_0_text_value", "LinkedIn Profile")]
    answers = build_answers(questions, candidate, profile)
    assert answers["job_application_answers_attributes_0_text_value"] == (
        "https://linkedin.com/in/ada"
    )


def test_unmatched_question_is_left_unanswered(candidate, profile) -> None:
    """Nothing plausible gets invented — this is what parks the application."""
    questions = [q("custom_1", "Describe a time you disagreed with a manager")]
    assert build_answers(questions, candidate, profile) == {}


def test_cover_letter_is_not_invented(candidate, profile) -> None:
    """Generated in Phase 3; until then it stays open rather than faked."""
    questions = [q("cover_letter", "Cover Letter", QuestionKind.COVER_LETTER)]
    assert build_answers(questions, candidate, profile) == {}


def test_select_matches_an_exact_option(candidate, profile) -> None:
    question = q(
        "auth",
        "Are you legally authorized to work in the US?",
        QuestionKind.SINGLE_SELECT,
        options=[Option(label="US citizen", value="citizen"), Option(label="No", value="no")],
    )
    answers = build_answers([question], candidate, profile)
    assert answers["auth"] == "citizen"


def test_select_without_an_exact_match_is_left_open(candidate, profile) -> None:
    """CLAUDE.md §2.2 — a near-miss on work authorization is not an answer."""
    question = q(
        "auth",
        "Work authorization status",
        QuestionKind.SINGLE_SELECT,
        options=[
            Option(label="Authorized without sponsorship", value="a"),
            Option(label="Require sponsorship", value="b"),
        ],
    )
    assert build_answers([question], candidate, profile) == {}


def test_owner_answers_win(candidate, profile) -> None:
    questions = [q("phone", "Phone", QuestionKind.PHONE)]
    answers = build_answers(questions, candidate, profile, extra={"phone": "+1-555-9999"})
    assert answers["phone"] == "+1-555-9999"


def test_owner_answer_fills_an_otherwise_unmappable_question(candidate, profile) -> None:
    """This is how approval-with-answers resumes a parked application."""
    questions = [q("custom_1", "Why do you want to work here?", QuestionKind.TEXTAREA)]
    answers = build_answers(questions, candidate, profile, extra={"custom_1": "I admire the work."})
    assert answers["custom_1"] == "I admire the work."


def test_resume_maps_only_when_a_path_is_supplied(candidate, profile) -> None:
    questions = [q("resume", "Resume/CV", QuestionKind.FILE)]

    assert build_answers(questions, candidate, profile) == {}

    answers = build_answers(questions, candidate, profile, resume_path="/tmp/cv.pdf")
    assert answers["resume"] == "/tmp/cv.pdf"


def test_empty_profile_values_are_not_filled(candidate) -> None:
    bare = Profile(id=uuid.uuid4(), candidate_id=uuid.uuid4(), label="bare", links_json={})
    questions = [q("phone", "Phone", QuestionKind.PHONE)]
    assert build_answers(questions, candidate, bare) == {}


def test_stored_answers_are_available_by_key(candidate, profile) -> None:
    profile.answers_kv_json = {"years_experience": "8"}
    values = profile_values(candidate, profile)
    assert values["years_experience"] == "8"


def test_salary_is_copied_verbatim(candidate, profile) -> None:
    questions = [q("salary", "Salary Expectations")]
    answers = build_answers(questions, candidate, profile)
    assert answers["salary"] == "$180,000"
