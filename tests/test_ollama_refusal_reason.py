"""When Ollama refuses a call, the owner is told why, in the daemon's words.

Found on 2026-10-07 by sending the local model a prompt longer than its
context. CLAUDE.md §14 said such a prompt "is cut by Ollama without an error".
On Ollama 0.40.0 it is refused: HTTP 400, and a body that names both numbers.

The provider reported "Client error '400 Bad Request' for url …", which is all
`raise_for_status` knows. The reason was in the body and was thrown away, so a
tailoring call that was too long for the local model read as a model that was
down. §7 records what that looks like from the review screen: a tailorer that
appears to do nothing.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from packages.llm import audit, timing
from packages.llm import provider as provider_module
from packages.llm.provider import LLMError, OllamaProvider, PromptTooLong

#: The body 0.40.0 sent, byte for byte: the daemon wraps its runner's JSON
#: error in a string.
TOO_LONG = {
    "error": json.dumps(
        {
            "error": {
                "code": 400,
                "message": (
                    "request (6076 tokens) exceeds the available context size "
                    "(4096 tokens), try increasing it"
                ),
                "type": "exceed_context_size_error",
                "n_prompt_tokens": 6076,
                "n_ctx": 4096,
            }
        }
    )
}


class _Answer(BaseModel):
    value: str


@pytest.fixture
def refusing(monkeypatch):
    """A daemon that answers `/api/chat` with a status and a body of the test's choosing."""

    def install(status: int, *, body: Any = None, text: str | None = None) -> None:
        async def post(self, url, *, json=None, headers=None, timeout=None):  # noqa: ANN001
            request = httpx.Request("POST", url)
            if url.endswith("/api/show"):
                return httpx.Response(200, json={"capabilities": []}, request=request)
            if text is not None:
                return httpx.Response(status, text=text, request=request)
            return httpx.Response(status, json=body, request=request)

        @asynccontextmanager
        async def stream(self, method, url, *, json=None, headers=None, timeout=None):  # noqa: ANN001
            request = httpx.Request(method, url)
            if text is not None:
                yield httpx.Response(status, text=text, request=request)
            else:
                yield httpx.Response(status, json=body, request=request)

        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        monkeypatch.setattr(httpx.AsyncClient, "stream", stream)

    provider_module._THINKING_MODELS.clear()
    yield install
    provider_module._THINKING_MODELS.clear()


def _local() -> OllamaProvider:
    return OllamaProvider("http://test", model="some-local-model")


async def test_a_prompt_longer_than_the_context_says_both_numbers(refusing) -> None:
    refusing(400, body=TOO_LONG)

    with pytest.raises(LLMError) as refused:
        await _local().complete("sys", "a very long prompt")

    message = str(refused.value)
    assert "6,076 tokens" in message
    assert "4,096" in message
    assert "OLLAMA_NUM_CTX" in message, "the setting that would change it is named"
    assert "400 Bad Request" not in message, "the status alone says nothing about why"


async def test_a_call_that_asks_for_json_is_told_the_same(refusing) -> None:
    refusing(400, body=TOO_LONG)

    with pytest.raises(LLMError, match=r"6,076 tokens.*4,096"):
        await _local().complete_json("sys", "a very long prompt", _Answer)


async def test_any_other_refusal_is_quoted(refusing) -> None:
    """A model that was never pulled. The daemon's sentence names it; the status does not."""
    refusing(404, body={"error": "model 'some-local-model' not found"})

    with pytest.raises(LLMError, match="model 'some-local-model' not found"):
        await _local().complete("sys", "usr")


async def test_a_refusal_with_nothing_readable_in_it_is_still_a_failure(refusing) -> None:
    refusing(502, text="<html>bad gateway</html>")

    with pytest.raises(LLMError, match="Ollama call failed: .*502"):
        await _local().complete("sys", "usr")


async def test_a_key_in_the_daemons_words_is_not_repeated(refusing) -> None:
    """§2.7. The reason is quoted, and a quoted reason is scrubbed like any other."""
    refusing(401, body={"error": "unauthorized for https://ollama.com/api/chat?key=sk-live-12345"})

    with pytest.raises(LLMError) as refused:
        await _local().complete("sys", "usr")

    assert "sk-live-12345" not in str(refused.value)
    assert "unauthorized" in str(refused.value)


async def test_only_the_start_of_a_long_reason_is_quoted(refusing) -> None:
    """Found in review. The daemon's words go into an exception that is logged
    and shown, and nothing promises they never repeat what they were sent. A
    reason is a sentence; whatever runs on past that is left behind (§10)."""
    echoed = "could not parse the request: " + "the whole of a résumé, repeated back " * 40
    refusing(400, body={"error": echoed})

    with pytest.raises(LLMError) as refused:
        await _local().complete("sys", "usr")

    message = str(refused.value)
    assert "could not parse the request" in message
    assert len(message) < 400
    assert message.endswith("…")


async def test_a_refused_call_is_in_the_audit_trail_and_has_no_timing(refusing) -> None:
    """The prompt was sent, so the trail has its line. Nothing was measured."""
    refusing(400, body=TOO_LONG)

    with pytest.raises(LLMError):
        await _local().complete("sys", "a very long prompt")

    assert len(audit.read_trail()) == 1
    assert timing.read_timings() == []


async def test_a_prompt_that_is_too_long_is_its_own_kind_of_failure(refusing) -> None:
    """So a caller can tell it from a model that is down. The advice for one is
    to start Ollama, and for the other it is to ask for less."""
    refusing(400, body=TOO_LONG)

    with pytest.raises(PromptTooLong):
        await _local().complete("sys", "a very long prompt")
    assert issubclass(PromptTooLong, LLMError), "every existing handler still catches it"


async def test_any_other_refusal_is_not_that_kind(refusing) -> None:
    refusing(404, body={"error": "model 'some-local-model' not found"})

    with pytest.raises(LLMError) as refused:
        await _local().complete("sys", "usr")
    assert not isinstance(refused.value, PromptTooLong)


async def test_a_refused_stream_says_why_too(refusing) -> None:
    """A streamed body is not read until it is asked for, and the reason is in
    the body. The dock asks through the stream, so without this every refusal
    there read "400 Bad Request" again."""
    refusing(400, body=TOO_LONG)

    with pytest.raises(PromptTooLong) as refused:
        async for _ in _local().stream("sys", "a very long prompt"):
            pass

    assert "6,076 tokens" in str(refused.value)
    assert "4,096" in str(refused.value)
