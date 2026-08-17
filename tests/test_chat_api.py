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
        async def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
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
