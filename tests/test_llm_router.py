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


# --- choosing between local and cloud — §7 with a §2.8 boundary --------------


def test_only_the_uploading_tasks_are_choosable() -> None:
    """§2.8 permits one third-party upload; the rest cannot be redirected.

    This is the whole safety property of the feature. Inbound-email
    classification reads recruiter correspondence and the assistant reads chat
    context — a setting able to point either at a cloud provider would be a way
    to opt out of a non-negotiable by editing `.env`.
    """
    assert set(router.CHOOSABLE_TASKS) == {
        "tailor_resume",
        "write_cover_letter",
        "answer_open_ended_question",
    }
    assert "classify_inbound_email" not in router.CHOOSABLE_TASKS
    assert "map_form_field" not in router.CHOOSABLE_TASKS


def test_a_locked_task_ignores_the_setting(monkeypatch) -> None:
    """Even set explicitly, the lock wins — it is a rule, not a default."""
    from packages.core.config import get_settings

    monkeypatch.setenv("LLM_TASK_TAILOR", "gemini")
    get_settings.cache_clear()

    assert router._chosen("classify_inbound_email") is None
    assert router._chosen("map_form_field") is None


def test_the_owner_can_pin_tailoring_to_the_local_model(monkeypatch) -> None:
    """Local tailoring without deleting the API key every other task wants."""
    from packages.core.config import get_settings

    monkeypatch.setenv("LLM_TASK_TAILOR", "ollama")
    get_settings.cache_clear()

    assert router._chosen("tailor_resume") == "ollama"


def test_auto_keeps_the_shipped_behaviour(monkeypatch) -> None:
    from packages.core.config import get_settings

    monkeypatch.setenv("LLM_TASK_TAILOR", "auto")
    get_settings.cache_clear()

    assert router._chosen("tailor_resume") is None


async def test_a_spent_quota_falls_back_to_the_local_model() -> None:
    """The case §7's own QuotaExceeded message describes as a manual step."""
    from packages.llm.quota import QuotaExceeded

    class Spent:
        name = "gemini"

        async def complete(self, system, user, **kwargs):
            raise QuotaExceeded("gemini", 200, 200)

    class Local:
        name = "ollama"
        model = "llama3.1"

        async def complete(self, system, user, **kwargs):
            return "tailored locally"

    fallback = router.FallbackProvider(Spent(), "tailor_resume")
    fallback._local = lambda: Local()

    answer = await fallback.complete("sys", "user")

    assert answer == "tailored locally"
    assert fallback.answered_by == "ollama:llama3.1"


async def test_the_fallback_is_never_the_stub() -> None:
    """§7: canned text must not reach a real application.

    StubProvider's marker exists so "nothing is configured" is visible in the
    diff. A fallback that reached it would hide exactly that.
    """
    assert router.LOCAL_PROVIDER == "ollama"
    assert router.LOCAL_PROVIDER != "stub"


async def test_an_unrelated_error_is_not_swallowed() -> None:
    """Only a spent quota or an unreachable provider retries locally.

    A bug in the prompt or the schema must surface, not quietly produce a
    second answer from a different model.
    """

    class Broken:
        name = "gemini"

        async def complete(self, system, user, **kwargs):
            raise ValueError("bad prompt")

    fallback = router.FallbackProvider(Broken(), "tailor_resume")
    fallback._local = lambda: pytest.fail("must not reach the local model")

    with pytest.raises(ValueError):
        await fallback.complete("sys", "user")


async def test_which_model_answered_is_recorded() -> None:
    """A résumé tailored locally after the allowance ran out is a different
    document from one tailored by the cloud model, and the owner approving it
    should be able to tell which they are looking at."""

    class Fine:
        name = "gemini"

        async def complete(self, system, user, **kwargs):
            return "tailored remotely"

    fallback = router.FallbackProvider(Fine(), "tailor_resume")

    await fallback.complete("sys", "user")

    assert fallback.answered_by == "gemini"
