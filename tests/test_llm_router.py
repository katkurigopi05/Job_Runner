"""Router safety boundaries — CLAUDE.md §2.2 and §7."""

from __future__ import annotations

import pytest

from packages.llm import router
from packages.llm.provider import LLMError


@pytest.mark.parametrize(
    "field",
    [
        "work_auth",
        "work_authorization",
        "employment_history",
        "salary_expectation",
        "needs_sponsorship",
        # An ATS renames the same question between boards. A protection that
        # only matches an exact key is not a protection.
        "work_authorization_status",
        "Work Auth",
        "candidate-work-auth",
    ],
)
def test_protected_fields_are_refused(field: str) -> None:
    """§2.2 — these answers are copied from the profile, never generated.

    A wrong work-authorization answer on a real application has legal
    consequences for the applicant.
    """
    with pytest.raises(router.ProtectedFieldError):
        router.answer_open_ended_question(field)


def test_the_field_argument_is_required() -> None:
    """The protection is structural, not opt-in.

    An optional argument is one forgotten keyword away from generating the
    answer it exists to prevent.
    """
    with pytest.raises(TypeError):
        router.answer_open_ended_question()  # type: ignore[call-arg]


def test_ordinary_questions_still_route() -> None:
    provider = router.answer_open_ended_question("why_us")
    assert provider is not None


def test_a_broken_preferred_provider_does_not_fall_back(monkeypatch) -> None:
    """A configured-but-unreachable provider is an error, not a stub.

    Silently substituting canned output would put stub-generated text on a
    real job application.
    """

    def explode(name: str | None = None):
        raise LLMError("ollama is not reachable")

    monkeypatch.setattr(router, "build_provider", explode)

    with pytest.raises(LLMError):
        router.classify_inbound_email()
