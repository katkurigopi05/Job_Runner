"""The local model, and the two things a request has to say for it to be usable.

The owner compared local models on the assistant's own prompts on 2026-10-06:
twelve questions, built from their data the way `/chat` builds them. `llama3.1`
gave five answers that were wrong or invented ("3 profiles need manual
completion", nine made-up kinds of security role, "all four postings are from
Stripe" under a list of five). Qwen3 8B at 3 bits gave none, in 4.82 GB against
5.26, and the owner switched to it.

Two things made that result, and neither was in the request the provider sent:

- **Thinking off.** Qwen3 reasons before it answers, and every caller here
  passes `max_tokens` as an *answer* budget. CLAUDE.md §7 records the same trap
  on OpenRouter: a 300-token allowance spent on thinking and an empty answer.
  Ollama says which models think (`/api/show`), so the switch is sent to those
  and to no others.
- **A capped context.** The memory a loaded model holds grows with its context,
  and on this 16 GB machine an uncapped one took 8.7 GB (the embedding run of
  2026-10-05). The measurement was made at 4,096, and the assistant's longest
  prompt is about 1,700 tokens.

A model Ollama hosts on its own servers is sent neither: its context costs this
machine nothing, and only the local path was measured.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel

from packages.core.config import Settings, get_settings
from packages.llm import provider as provider_module
from packages.llm.provider import OllamaCloudProvider, OllamaProvider

QWEN_3BIT = "hf.co/unsloth/Qwen3-8B-GGUF:Q3_K_M"


class _Answer(BaseModel):
    value: str


class _Ollama:
    """Stands in for the daemon: records what was posted, answers by path."""

    def __init__(self, capabilities: list[str] | None, *, show_fails: bool = False) -> None:
        self.capabilities = capabilities
        self.show_fails = show_fails
        self.posted: list[tuple[str, dict]] = []

    async def post(self, url, *, json=None, headers=None, timeout=None):  # noqa: ANN001
        self.posted.append((url, json))
        if url.endswith("/api/show"):
            if self.show_fails:
                raise httpx.ConnectError("no daemon")
            return _Response({"capabilities": self.capabilities or []})
        content = '{"value": "ok"}' if json.get("format") == "json" else "ok"
        return _Response({"message": {"content": content}})

    def chats(self) -> list[dict]:
        return [body for url, body in self.posted if url.endswith("/api/chat")]

    def shows(self) -> int:
        return sum(1 for url, _ in self.posted if url.endswith("/api/show"))


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def ollama(monkeypatch):
    def install(capabilities: list[str] | None, **kwargs) -> _Ollama:
        fake = _Ollama(capabilities, **kwargs)

        async def post(self, url, **kw):  # noqa: ANN001
            return await fake.post(url, **kw)

        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        return fake

    provider_module._THINKING_MODELS.clear()
    yield install
    provider_module._THINKING_MODELS.clear()


def test_the_default_local_model_is_the_one_that_was_measured() -> None:
    """The field's own default, whatever this machine's `.env` says."""
    assert Settings.model_fields["ollama_model"].default == QWEN_3BIT


async def test_a_local_call_caps_the_context(ollama) -> None:
    daemon = ollama(["completion"])

    await OllamaProvider("http://test", model="some-model").complete("sys", "usr", max_tokens=600)

    options = daemon.chats()[0]["options"]
    assert options["num_ctx"] == get_settings().ollama_num_ctx == 4096
    assert options["num_predict"] == 600, "the answer budget is still the caller's"


async def test_thinking_is_switched_off_for_a_model_that_thinks(ollama) -> None:
    daemon = ollama(["completion", "tools", "thinking"])

    await OllamaProvider("http://test", model=QWEN_3BIT).complete("sys", "usr")

    assert daemon.chats()[0]["think"] is False


async def test_a_model_that_does_not_think_is_not_sent_the_switch(ollama) -> None:
    daemon = ollama(["completion", "tools"])

    await OllamaProvider("http://test", model="llama3.1").complete("sys", "usr")

    assert "think" not in daemon.chats()[0]


async def test_the_daemon_is_asked_once_per_model(ollama) -> None:
    daemon = ollama(["completion", "thinking"])

    for _ in range(3):
        # A new provider each time, as `/chat` builds one per question.
        await OllamaProvider("http://test", model=QWEN_3BIT).complete("sys", "usr")
    await OllamaProvider("http://test", model="another").complete("sys", "usr")

    assert daemon.shows() == 2
    assert [body.get("think") for body in daemon.chats()] == [False, False, False, False]


async def test_an_unanswered_question_about_thinking_does_not_fail_the_call(ollama) -> None:
    daemon = ollama(None, show_fails=True)

    answer = await OllamaProvider("http://test", model=QWEN_3BIT).complete("sys", "usr")

    assert answer == "ok"
    assert "think" not in daemon.chats()[0]
    assert not provider_module._THINKING_MODELS, "a failure is not remembered as an answer"


async def test_a_json_call_is_shaped_the_same_way(ollama) -> None:
    daemon = ollama(["completion", "thinking"])

    got = await OllamaProvider("http://test", model=QWEN_3BIT).complete_json("sys", "usr", _Answer)

    body = daemon.chats()[0]
    assert got.value == "ok"
    assert body["format"] == "json"
    assert body["think"] is False
    assert body["options"]["num_ctx"] == 4096


async def test_a_hosted_model_is_left_as_it_was(ollama) -> None:
    daemon = ollama(["completion", "thinking"])

    await OllamaCloudProvider("http://test", model="glm-5.3-flash:cloud").complete("sys", "usr")

    body = daemon.chats()[0]
    assert "num_ctx" not in body["options"]
    assert "think" not in body
    assert daemon.shows() == 0


def test_the_refusal_of_a_cloud_tag_names_the_real_default() -> None:
    """It said "(llama3.1 is the default)" in a string, beside a setting that moved."""
    from pathlib import Path

    source = Path("apps/api/routers/chat.py").read_text(encoding="utf-8")
    assert "llama3.1 is the default" not in source
