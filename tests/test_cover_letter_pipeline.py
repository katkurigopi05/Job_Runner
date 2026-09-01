"""The cover letter reaching an actual application.

CLAUDE.md §15 recorded this as the gap: the module wrote and vetted letters
that nothing ever asked for, and `Application.cover_letter_ref` was never
written. The tests worth having are the ones proving it declines quietly —
a letter is never worth failing an apply for.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from apps.worker import apply_job
from packages.ats.base import Question, QuestionKind
from packages.core.models import Application, Profile, Resume
from packages.tailor.parse import parse_text

RESUME = """Jane Doe
jane@example.com

Experience
Acme Corp - Senior Backend Engineer
- Maintained the billing service and reduced invoice errors by 30%.
- Built a Python and PostgreSQL pipeline handling 12 million events a day.

Skills
Python, PostgreSQL, Docker
"""


class _Posting:
    description_raw = "Backend engineer wanted. Python and PostgreSQL."
    company_name = "Globex"


def _questions(*kinds: QuestionKind) -> list[Question]:
    return [Question(key=f"f{i}", label=f"field {i}", kind=kind) for i, kind in enumerate(kinds)]


def _application() -> Application:
    return Application(
        id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        profile_id=uuid.uuid4(),
        url="https://boards.greenhouse.io/acme/jobs/1",
        ats="greenhouse",
        status="running",
    )


class _Session:
    """Only `get(Resume, ...)` is reached by the code under test."""

    def __init__(self, resume: Resume | None) -> None:
        self._resume = resume

    async def get(self, model: Any, identifier: Any) -> Any:
        return self._resume


def _profile(with_resume: bool = True) -> Profile:
    return Profile(
        id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        label="default",
        base_resume_id=uuid.uuid4() if with_resume else None,
    )


def _resume() -> Resume:
    return Resume(
        id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        version=1,
        storage_ref="resumes/jane.pdf",
        parsed_json=parse_text(RESUME).model_dump(mode="json"),
    )


async def test_no_cover_letter_field_means_no_letter(monkeypatch) -> None:
    """Most forms have none. Writing one anyway spends a model call and, on a
    remote provider, a §2.8 upload of the résumé — for something no employer
    asked for."""

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("wrote a letter nobody asked for")

    monkeypatch.setattr(apply_job.llm_router, "write_cover_letter", fail)

    letter = await apply_job._cover_letter(
        _Session(_resume()),
        _application(),
        _profile(),
        _Posting(),
        _questions(QuestionKind.TEXT, QuestionKind.FILE),
    )

    assert letter is None


async def test_no_resume_means_no_letter() -> None:
    """The letter is grounded in the same corpus the tailorer uses."""
    letter = await apply_job._cover_letter(
        _Session(None),
        _application(),
        _profile(with_resume=False),
        _Posting(),
        _questions(QuestionKind.COVER_LETTER),
    )

    assert letter is None


async def test_a_refused_letter_leaves_the_field_unanswered(monkeypatch) -> None:
    """write() never falls back, so a refusal means no letter and the field
    goes to the owner under §2.4 — exactly as it did before this existed."""
    from packages.tailor.cover import CoverLetter

    async def refuse(*args: Any, **kwargs: Any) -> CoverLetter:
        return CoverLetter(rejected_reason="claims a credential the résumé lacks")

    monkeypatch.setattr("packages.tailor.cover.write", refuse)

    application = _application()
    letter = await apply_job._cover_letter(
        _Session(_resume()),
        application,
        _profile(),
        _Posting(),
        _questions(QuestionKind.COVER_LETTER),
    )

    assert letter is None
    assert application.cover_letter_ref is None


async def test_a_provider_failure_does_not_fail_the_application(monkeypatch) -> None:
    """A letter is never worth failing an apply for."""

    async def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("model down")

    monkeypatch.setattr("packages.tailor.cover.write", explode)

    letter = await apply_job._cover_letter(
        _Session(_resume()),
        _application(),
        _profile(),
        _Posting(),
        _questions(QuestionKind.COVER_LETTER),
    )

    assert letter is None


# `test_an_accepted_letter_is_stored_and_referenced` lived here and was removed
# in the merge with main. It asserted that an accepted letter is stored and
# `cover_letter_ref` written, against hand-built session and posting stubs that
# modelled the string-returning `_cover_letter` this branch had. main's version
# publishes the file, records which model answered, and reuses a stored letter,
# and `tests/test_apply_cover_letter.py::test_a_vetted_letter_is_stored_and_referenced`
# already makes the same assertion against a real session. Two stubs of one
# implementation is how the weaker one goes stale unnoticed.


def test_the_letter_reaches_the_form_only_when_supplied() -> None:
    """build_answers fills the field from what it was handed, and leaves it
    alone otherwise — it never invents one."""
    from packages.ats.answers import build_answers
    from packages.core.models import Candidate

    candidate = Candidate(id=uuid.uuid4(), user_id=uuid.uuid4(), name="Jane", email="j@x.com")
    profile = _profile()
    questions = [Question(key="cl", label="Cover letter", kind=QuestionKind.COVER_LETTER)]

    without = build_answers(questions, candidate, profile)
    with_letter = build_answers(questions, candidate, profile, cover_letter_text="Dear Globex")

    assert "cl" not in without
    assert with_letter["cl"] == "Dear Globex"


@pytest.mark.parametrize("kind", [QuestionKind.TEXTAREA, QuestionKind.TEXT])
def test_a_plain_textarea_is_not_a_cover_letter_field(kind: QuestionKind) -> None:
    """Only a field the adapter classified as a cover letter takes one."""
    from packages.ats.answers import build_answers
    from packages.core.models import Candidate

    candidate = Candidate(id=uuid.uuid4(), user_id=uuid.uuid4(), name="Jane", email="j@x.com")
    questions = [Question(key="q", label="Tell us about a project", kind=kind)]

    answers = build_answers(questions, candidate, _profile(), cover_letter_text="Dear Globex")

    assert answers.get("q") != "Dear Globex"
