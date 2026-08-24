"""The assistant — CLAUDE.md §2.2 and §2.8 applied to a chat surface."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from packages.llm.provider import LLMError, StubProvider


@pytest.mark.parametrize(
    "question",
    [
        "what should I put for work authorization?",
        "draft my employment history section",
        "what salary_expectation should I give them?",
        "do I need sponsorship, what do I say",
    ],
)
async def test_protected_questions_are_refused(client: AsyncClient, question: str) -> None:
    """§2.2 — refused by code, not by asking the model nicely.

    A system prompt is a request. This is a rule, so it is enforced before the
    model is reached at all.
    """
    answered = await client.post("/chat", json={"message": question})

    assert answered.status_code == 200
    body = answered.json()
    assert body["provider"] == "refused"
    assert body["grounded"] is False
    assert "profile" in body["reply"].lower()


async def test_the_assistant_never_uses_a_cloud_provider(client: AsyncClient, monkeypatch) -> None:
    """§2.8 — chat context is the owner's data and is not the tailoring call.

    With a cloud provider configured and Ollama down, the honest outcome is an
    error. Falling back would upload application URLs and recruiter mail to a
    third party the owner never agreed to for this.
    """
    import apps.api.routers.chat as chat_module

    asked: list[str] = []

    def only_local(name: str | None = None):
        asked.append(name or "default")
        raise LLMError("connection refused")

    monkeypatch.setattr(chat_module.llm_router, "build_provider", only_local)

    answered = await client.post("/chat", json={"message": "how many are waiting on me?"})

    assert answered.status_code == 500
    assert "locally on purpose" in answered.json()["error"]["message"]
    # Asked for ollama by name, and never retried with anything else.
    assert asked == ["ollama"]


async def test_the_answer_is_grounded_in_real_counts(client: AsyncClient, monkeypatch) -> None:
    """The model is handed facts, not left to invent a status."""
    import apps.api.routers.chat as chat_module

    seen: dict[str, str] = {}

    class Recorder(StubProvider):
        async def complete(
            self,
            system: str,
            user: str,
            *,
            max_tokens: int = 1024,
            temperature: float = 0.2,
        ) -> str:
            seen["user"] = user
            return "three are waiting"

    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: Recorder())

    answered = await client.post("/chat", json={"message": "what needs me?"})

    assert answered.status_code == 200
    assert answered.json()["reply"] == "three are waiting"
    assert "APPLICATIONS BY STATUS:" in seen["user"]


async def test_empty_message_is_rejected(client: AsyncClient) -> None:
    answered = await client.post("/chat", json={"message": "   "})
    assert answered.status_code == 400


@pytest.mark.parametrize("model", ["kimi-k2.6:cloud", "qwen3-coder:480b-cloud"])
async def test_a_cloud_model_is_refused_even_though_the_provider_is_ollama(
    client: AsyncClient, monkeypatch, model: str
) -> None:
    """§2.8 — "ollama" stopped being enough to know the answer stayed here.

    Ollama serves cloud-hosted models over the same localhost API. Both of
    these are in the owner's `ollama list`, neither runs on this machine, and
    nothing in the base URL distinguishes them. Since OLLAMA_MODEL became
    settable, one edit to `.env` could send applications and recruiter mail to
    a third party while the reply still said `provider="ollama"`.

    Note the two spell it differently, `:cloud` against `-cloud`, which is why
    `audit.is_local` matches the substring rather than a suffix.
    """
    import apps.api.routers.chat as chat_module

    class CloudModel(StubProvider):
        def __init__(self) -> None:
            super().__init__()
            self.model = model

    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: CloudModel())

    answered = await client.post("/chat", json={"message": "how many are waiting on me?"})

    assert answered.status_code == 400
    message = answered.json()["error"]["message"]
    assert model in message
    assert "locally" in message.lower()


async def test_a_pulled_local_model_is_not_refused(client: AsyncClient, monkeypatch) -> None:
    """The guard must not reject the models that actually run here."""
    import apps.api.routers.chat as chat_module

    class LocalModel(StubProvider):
        def __init__(self) -> None:
            super().__init__()
            self.model = "llama3.1"
            self.responses = {"": "Three are waiting on you."}

    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: LocalModel())

    answered = await client.post("/chat", json={"message": "how many are waiting on me?"})

    assert answered.status_code == 200
    assert answered.json()["provider"] == "ollama"


def test_the_configured_model_is_the_one_that_is_asked_for(monkeypatch) -> None:
    """OLLAMA_MODEL was named in three files and settable in none."""
    from packages.core.config import get_settings
    from packages.llm.provider import OllamaProvider

    get_settings.cache_clear()
    assert OllamaProvider().model == get_settings().ollama_model

    monkeypatch.setenv("OLLAMA_MODEL", "mistral")
    assert OllamaProvider().model == "mistral"
