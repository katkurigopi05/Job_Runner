"""When the local model does not answer, the owner is told which thing is wrong.

Three failures read alike and need different hands. Ollama is not running:
start it. Ollama is running and does not have the model: pull it. Ollama is
running, has it, and did not reply in time: wait. All three used to come back
as one sentence carrying both of the first two remedies, so a missing model
told the owner to start a daemon that was up, and a stopped daemon told them
to pull a model they had.

Measured against Ollama 0.40.0 on 2026-10-10: a model it does not have is HTTP
404 with `{"error": "model '<tag>' not found"}`, from `/api/chat` streamed or
not; nothing listening is httpx's "All connection attempts failed".

What must not change with any of them: nothing falls back to another model.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from httpx import AsyncClient

from packages.llm import provider as provider_module
from packages.llm.provider import (
    LLMError,
    ModelMissing,
    ModelTimedOut,
    ModelUnreachable,
    OllamaProvider,
    PromptTooLong,
)

#: The body 0.40.0 sent for a tag it does not have.
NOT_FOUND = {"error": "model 'some-local-model' not found"}


@pytest.fixture
def daemon(monkeypatch):
    """A daemon that fails `/api/chat` in the way the test chooses."""

    def install(*, raises: Exception | None = None, status: int = 200, body: Any = None) -> None:
        async def post(self, url, *, json=None, headers=None, timeout=None):  # noqa: ANN001
            request = httpx.Request("POST", url)
            if url.endswith("/api/show"):
                return httpx.Response(200, json={"capabilities": []}, request=request)
            if raises is not None:
                raise raises
            return httpx.Response(status, json=body, request=request)

        @asynccontextmanager
        async def stream(self, method, url, *, json=None, headers=None, timeout=None):  # noqa: ANN001
            if raises is not None:
                raise raises
            yield httpx.Response(status, json=body, request=httpx.Request(method, url))

        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        monkeypatch.setattr(httpx.AsyncClient, "stream", stream)

    provider_module._THINKING_MODELS.clear()
    yield install
    provider_module._THINKING_MODELS.clear()


def _local() -> OllamaProvider:
    return OllamaProvider("http://test", model="some-local-model")


async def _ask(how: str) -> None:
    if how == "complete":
        await _local().complete("sys", "usr")
        return
    async for _ in _local().stream("sys", "usr"):
        pass


BOTH_CALLS = pytest.mark.parametrize("how", ["complete", "stream"])


# --------------------------------------------------------------------------
# The provider says which
# --------------------------------------------------------------------------


@BOTH_CALLS
async def test_nothing_listening_is_its_own_kind_of_failure(daemon, how) -> None:
    daemon(raises=httpx.ConnectError("All connection attempts failed"))

    with pytest.raises(ModelUnreachable) as failed:
        await _ask(how)

    assert "All connection attempts failed" in str(failed.value)


@BOTH_CALLS
async def test_a_model_the_daemon_does_not_have_is_its_own_kind(daemon, how) -> None:
    daemon(status=404, body=NOT_FOUND)

    with pytest.raises(ModelMissing) as failed:
        await _ask(how)

    assert "model 'some-local-model' not found" in str(failed.value)


@BOTH_CALLS
async def test_a_daemon_that_does_not_reply_in_time_is_its_own_kind(daemon, how) -> None:
    """httpx's read timeout carries no words at all."""
    daemon(raises=httpx.ReadTimeout(""))

    with pytest.raises(ModelTimedOut):
        await _ask(how)


async def test_a_daemon_that_cannot_be_connected_to_in_time_is_unreachable(daemon) -> None:
    """Not a slow answer: nothing took the connection."""
    daemon(raises=httpx.ConnectTimeout(""))

    with pytest.raises(ModelUnreachable):
        await _ask("complete")


async def test_a_404_that_is_not_about_the_model_is_not_a_missing_model(daemon) -> None:
    """A wrong base URL answers 404 too, and pulling a model would not fix it."""
    daemon(status=404, body={"error": "404 page not found"})

    with pytest.raises(LLMError) as failed:
        await _ask("complete")

    assert not isinstance(failed.value, ModelMissing | ModelUnreachable | ModelTimedOut)


def test_each_kind_is_still_a_failure_of_the_model() -> None:
    """Callers that catch `LLMError` catch these, and none is the too-long kind."""
    for kind in (ModelUnreachable, ModelMissing, ModelTimedOut):
        assert issubclass(kind, LLMError) and not issubclass(kind, PromptTooLong)


# --------------------------------------------------------------------------
# The assistant gives the remedy for that one, on both routes
# --------------------------------------------------------------------------


class _Fails:
    name = "ollama"
    model = "some-local-model"
    base_url = "http://localhost:11434"

    def __init__(self, error: LLMError) -> None:
        self._error = error

    async def complete(self, system: str, user: str, *, max_tokens: int, temperature: float) -> str:
        raise self._error

    async def stream(self, system: str, user: str, *, max_tokens: int, temperature: float):  # noqa: ANN201
        raise self._error
        yield ""  # pragma: no cover - makes this an async generator


@pytest.fixture
def failing(monkeypatch):
    """The local model failing one way, and every provider that was asked for."""
    import apps.api.routers.chat as chat_module

    asked: list[str | None] = []

    def install(error: LLMError) -> list[str | None]:
        def build(name: str | None = None) -> _Fails:
            asked.append(name)
            return _Fails(error)

        monkeypatch.setattr(chat_module.llm_router, "build_provider", build)
        return asked

    return install


BOTH_ROUTES = pytest.mark.parametrize("route", ["/chat", "/chat/stream"])
QUESTION = {"message": "how many are waiting on me?"}


async def _message(client: AsyncClient, route: str) -> str:
    answered = await client.post(route, json=QUESTION)
    assert answered.status_code == 500
    return str(answered.json()["error"]["message"])


@BOTH_ROUTES
async def test_a_daemon_that_is_not_running_says_to_start_it(client, failing, route) -> None:
    asked = failing(
        ModelUnreachable(
            "Ollama call failed: All connection attempts failed",
            base_url="http://localhost:11434",
        )
    )

    message = await _message(client, route)

    assert "ollama serve" in message
    assert "http://localhost:11434" in message, "the address that was tried"
    assert "ollama pull" not in message, "the model may well be there"
    assert "Nothing was sent anywhere else" in message
    assert asked == ["ollama"], "no other provider was built to answer in its place"


@BOTH_ROUTES
async def test_a_model_that_is_not_pulled_says_to_pull_it(client, failing, route) -> None:
    asked = failing(
        ModelMissing(
            "Ollama call failed: model 'some-local-model' not found", model="some-local-model"
        )
    )

    message = await _message(client, route)

    assert "ollama pull some-local-model" in message, "the model that was asked for"
    assert "ollama serve" not in message, "Ollama answered: it is running"
    assert "Nothing was sent anywhere else" in message
    assert asked == ["ollama"]


@BOTH_ROUTES
async def test_a_daemon_that_was_too_slow_says_neither(client, failing, route) -> None:
    asked = failing(ModelTimedOut("Ollama call failed: ReadTimeout"))

    message = await _message(client, route)

    assert "ollama serve" not in message and "ollama pull" not in message
    assert "did not reply in time" in message
    assert "Nothing was sent anywhere else" in message
    assert asked == ["ollama"]


@BOTH_ROUTES
async def test_a_failure_of_no_known_kind_still_offers_both(client, failing, route) -> None:
    """What it said before, for a failure this cannot tell apart."""
    failing(LLMError("Ollama call failed: something else"))

    message = await _message(client, route)

    assert "The local model is not answering" in message
    assert "ollama serve" in message and "ollama pull" in message
    assert "Nothing was sent anywhere else" in message


@BOTH_ROUTES
@pytest.mark.parametrize("kind", [ModelUnreachable, ModelMissing, ModelTimedOut])
async def test_every_kind_opens_the_same_way(client, failing, route, kind) -> None:
    """The dock and its proxy test read this sentence to know which failure it is."""
    failing(kind("Ollama call failed: whatever it was"))

    assert "The local model is not answering" in await _message(client, route)
