"""Ollama's hosted models, and the §14 line they are on the far side of.

CLAUDE.md §14 refuses a `:cloud` tag on `OLLAMA_MODEL` because Ollama serves
hosted models over the identical localhost API, so a remote answer would carry
a local label. The owner asked for `glm-5.3-flash:cloud` anyway, and the way it
landed is a *separately named provider* rather than a loosened check: the
refusal still fires, and choosing `ollama_cloud` is the informed decision the
rule was protecting.

These tests hold that shape. The ones that matter most are the negatives —
what must stay unreachable without someone typing the name.
"""

from __future__ import annotations

import pytest

from packages.llm import router
from packages.llm.audit import is_local
from packages.llm.provider import (
    LLMError,
    OllamaCloudProvider,
    OllamaProvider,
    build_provider,
)

CLOUD_MODEL = "glm-5.3-flash:cloud"


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_CLOUD_MODEL", CLOUD_MODEL)


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------


def test_it_defaults_to_the_model_the_owner_asked_for() -> None:
    assert OllamaCloudProvider().model == CLOUD_MODEL


def test_the_model_is_settable(configured: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_CLOUD_MODEL", "glm-4.7-flash:cloud")
    assert OllamaCloudProvider().model == "glm-4.7-flash:cloud"


def test_it_refuses_a_model_that_does_not_run_remotely() -> None:
    """The mirror of §14, and the reason both refusals exist.

    `ollama` refusing a cloud tag stops a remote call being logged as local.
    This stops a local call being logged as remote — an audit trail that cries
    wolf is one the owner stops reading, and §2.8's value is that it is read.
    """
    with pytest.raises(LLMError, match="not one of the models Ollama hosts remotely"):
        OllamaCloudProvider(model="llama3.1")


def test_the_refusal_names_the_way_out() -> None:
    with pytest.raises(LLMError, match="OLLAMA_MODEL"):
        OllamaCloudProvider(model="llama3.1")


def test_it_shares_the_ollama_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """The confusion this whole design exists to handle, asserted outright."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    assert OllamaCloudProvider().base_url == OllamaProvider().base_url


def test_no_credential_is_sent_by_default() -> None:
    """The signed-in local daemon proxies these; there is nothing to send."""
    assert OllamaCloudProvider()._headers() == {}


def test_a_key_is_sent_when_one_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-test")
    assert OllamaCloudProvider()._headers() == {"Authorization": "Bearer sk-test"}


def test_the_local_provider_still_sends_nothing() -> None:
    assert OllamaProvider()._headers() == {}


def test_build_provider_knows_the_name() -> None:
    assert isinstance(build_provider("ollama_cloud"), OllamaCloudProvider)


# ---------------------------------------------------------------------------
# The audit trail
# ---------------------------------------------------------------------------


def test_a_hosted_call_is_recorded_as_having_left() -> None:
    assert is_local("ollama_cloud", CLOUD_MODEL) is False


def test_the_label_does_not_depend_on_the_tag() -> None:
    """Correct by construction: the provider name decides, not a substring.

    Under the name `ollama` a call is presumed local and only "cloud" in the
    tag rescues it. Under this name it is presumed remote, which is right for
    something whose purpose is running elsewhere — and survives a retag.
    """
    assert is_local("ollama_cloud", "anything-at-all") is False


def test_the_local_provider_is_unchanged() -> None:
    assert is_local("ollama", "llama3.1") is True
    assert is_local("ollama", "kimi-k2.6:cloud") is False


# ---------------------------------------------------------------------------
# Opt-in — the negatives
# ---------------------------------------------------------------------------


def test_it_is_unreachable_until_a_model_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_CLOUD_MODEL", raising=False)
    from packages.core.config import get_settings

    get_settings.cache_clear()
    assert router._configured("ollama_cloud") is False


def test_naming_a_model_makes_it_reachable(configured: None) -> None:
    assert router._configured("ollama_cloud") is True


def test_it_is_never_chosen_automatically(configured: None) -> None:
    """The one that matters most.

    Every other remote provider needs an API key, whose absence keeps it out of
    "auto". This one needs no key — so if it were in QUALITY_ORDER, a single
    line in `.env` would send every task off the machine with nothing in the
    configuration reading as having asked for that.
    """
    assert "ollama_cloud" not in router.QUALITY_ORDER
    assert router.best_available() != "ollama_cloud"


def test_it_does_not_become_the_tailoring_cloud_by_default(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cloud_for_tailoring` walks QUALITY_ORDER, so it must not surface here."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("LLM_TASK_TAILOR", "auto")
    from packages.core.config import get_settings

    get_settings.cache_clear()
    assert router.cloud_for_tailoring() != "ollama_cloud"


def test_it_answers_when_named_for_a_task(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_TASK_TAILOR", "ollama_cloud")
    from packages.core.config import get_settings

    get_settings.cache_clear()
    assert router.cloud_for_tailoring() == "ollama_cloud"


def test_it_may_be_the_remote_half_of_a_comparison(configured: None) -> None:
    assert router.is_comparable_cloud("ollama_cloud") is True
    assert "ollama_cloud" in router.comparable_clouds()


def test_an_unconfigured_provider_is_not_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_CLOUD_MODEL", raising=False)
    from packages.core.config import get_settings

    get_settings.cache_clear()
    assert "ollama_cloud" not in router.comparable_clouds()


def test_the_assistant_is_still_not_a_choosable_task() -> None:
    """§7's list is unchanged. Adding a provider must not widen it.

    The assistant is chosen per question in the UI, never by an environment
    variable — the `.env` opt-out route §7 warns about stays closed.
    """
    assert set(router.CHOOSABLE_TASKS) == {
        "tailor_resume",
        "write_cover_letter",
        "answer_open_ended_question",
    }
