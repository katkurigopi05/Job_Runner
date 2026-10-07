"""The local model's answer, a piece at a time.

The assistant waited for the whole answer before showing any of it. Measured
on the owner's machine (CLAUDE.md §14): about 5 s before the local model's
first word and about 12 s for the answer. Ollama can send the words as it
writes them; the provider asked it not to.

What must not change by asking for a stream: the request is the same request
(context cap, answer budget, thinking off), the audit trail gets its one line
before anything is sent, and a failure is still an `LLMError`.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from packages.core.config import get_settings
from packages.llm import audit, timing
from packages.llm import provider as provider_module
from packages.llm.provider import (
    LLMError,
    OllamaCloudProvider,
    OllamaProvider,
    StubProvider,
    stream_text,
)

QWEN_3BIT = "hf.co/unsloth/Qwen3-8B-GGUF:Q3_K_M"


def _line(text: str, **more: Any) -> str:
    return json.dumps({"message": {"role": "assistant", "content": text}, "done": False, **more})


#: What the daemon sends for "Two are waiting.": pieces, then a closing line
#: that carries no text and the totals.
ANSWER = [
    _line("Two"),
    _line(" are"),
    "",
    _line(" waiting."),
    json.dumps({"message": {"content": ""}, "done": True, "eval_count": 4}),
    _line(" (never read: the stream said it was done)"),
]


class _Streamed:
    def __init__(self, lines: list[str], status: int = 200) -> None:
        self._lines = lines
        self.status_code = status
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("model not found", request=None, response=None)  # type: ignore[arg-type]

    async def aread(self) -> bytes:
        return b""

    async def aiter_lines(self):  # noqa: ANN201
        for line in self._lines:
            yield line


class _Daemon:
    """Stands in for Ollama: `/api/show` by post, `/api/chat` by stream."""

    def __init__(self, lines: list[str], *, thinks: bool = False, status: int = 200) -> None:
        self.lines = lines
        self.thinks = thinks
        self.status = status
        self.streamed: list[dict[str, Any]] = []
        self.response: _Streamed | None = None


@pytest.fixture
def daemon(monkeypatch):
    def install(lines: list[str], **kwargs: Any) -> _Daemon:
        fake = _Daemon(lines, **kwargs)

        class _Shown:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                return {"capabilities": ["completion", *(["thinking"] if fake.thinks else [])]}

        async def post(self, url, **kw):  # noqa: ANN001
            assert url.endswith("/api/show"), "the answer must be asked for as a stream"
            return _Shown()

        @asynccontextmanager
        async def stream(self, method, url, *, json=None, headers=None, timeout=None):  # noqa: ANN001
            assert (method, url.rsplit("/api/", 1)[1]) == ("POST", "chat")
            fake.streamed.append(json)
            fake.response = _Streamed(fake.lines, fake.status)
            try:
                yield fake.response
            finally:
                fake.response.closed = True

        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        monkeypatch.setattr(httpx.AsyncClient, "stream", stream)
        return fake

    provider_module._THINKING_MODELS.clear()
    yield install
    provider_module._THINKING_MODELS.clear()


async def _pieces(provider: Any, **kwargs: Any) -> list[str]:
    return [piece async for piece in provider.stream("sys", "usr", **kwargs)]


async def test_the_answer_arrives_in_the_pieces_the_daemon_wrote(daemon) -> None:
    daemon(ANSWER)

    pieces = await _pieces(OllamaProvider("http://test", model="some-model"))

    assert pieces == ["Two", " are", " waiting."]
    assert "".join(pieces) == "Two are waiting."


async def test_it_is_the_same_request_with_the_stream_asked_for(daemon) -> None:
    """Everything `test_ollama_local_model.py` holds for a plain call."""
    fake = daemon(ANSWER, thinks=True)

    await _pieces(OllamaProvider("http://test", model=QWEN_3BIT), max_tokens=600, temperature=0.2)

    [body] = fake.streamed
    assert body["stream"] is True
    assert body["think"] is False, "a thinking model would spend the answer budget reasoning"
    assert body["options"] == {
        "num_predict": 600,
        "temperature": 0.2,
        "num_ctx": get_settings().ollama_num_ctx,
    }
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


async def test_a_plain_call_still_asks_for_no_stream(daemon, monkeypatch) -> None:
    posted: list[dict[str, Any]] = []

    class _Whole:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"capabilities": [], "message": {"content": "whole"}}

    async def post(self, url, *, json=None, headers=None, timeout=None):  # noqa: ANN001
        posted.append(json)
        return _Whole()

    monkeypatch.setattr(httpx.AsyncClient, "post", post)

    assert await OllamaProvider("http://test", model="some-model").complete("sys", "usr") == "whole"
    assert posted[-1]["stream"] is False


async def test_the_audit_trail_gets_its_line_before_anything_is_read(daemon) -> None:
    """§2.8. One line for the call, written whether or not the answer is read."""
    daemon(ANSWER)
    stream = OllamaProvider("http://test", model="some-model").stream("sys", "usr")

    assert audit.read_trail() == [], "nothing is recorded until the call is made"
    assert await anext(stream) == "Two"
    [entry] = audit.read_trail()
    assert entry.matches("sys", "usr")
    assert (entry.provider, entry.left_machine) == ("ollama", False)

    await stream.aclose()
    assert len(audit.read_trail()) == 1


async def test_a_reader_that_stops_closes_the_connection(daemon) -> None:
    """A closed dock must stop the model, or it writes an answer for nobody."""
    fake = daemon(ANSWER)
    stream = OllamaProvider("http://test", model="some-model").stream("sys", "usr")

    await anext(stream)
    assert fake.response is not None and not fake.response.closed
    await stream.aclose()

    assert fake.response.closed


async def test_closing_the_wrapper_closes_the_connection_too(daemon) -> None:
    """Found in review. The route reads through `stream_text`, and closing a
    generator that is iterating another one does not close the inner one: the
    connection stayed open until the garbage collector reached it, and the
    model kept writing for that long."""
    fake = daemon(ANSWER)
    local = OllamaProvider("http://test", model="some-model")
    pieces = stream_text(local, "sys", "usr", max_tokens=600)

    assert await anext(pieces) == "Two"
    assert fake.response is not None and not fake.response.closed
    await pieces.aclose()

    assert fake.response.closed


async def test_an_answer_nobody_reads_is_closed_when_it_is_let_go(daemon) -> None:
    """The route waits for the first piece before it answers. If the reader has
    gone by then, nothing ever iterates the rest, so nothing closes it by hand:
    it is closed when the last reference to it is dropped. That is the only
    closer this case has, so it is held here."""
    import asyncio
    import gc

    fake = daemon(ANSWER)
    pieces = stream_text(OllamaProvider("http://test", model="some-model"), "sys", "usr")
    assert await anext(pieces) == "Two"
    assert fake.response is not None and not fake.response.closed

    del pieces
    gc.collect()
    for _ in range(5):
        await asyncio.sleep(0)

    assert fake.response.closed


async def test_an_error_line_is_an_error(daemon) -> None:
    daemon([_line("Two"), json.dumps({"error": "model runner has unexpectedly stopped"})])
    stream = OllamaProvider("http://test", model="some-model").stream("sys", "usr")

    assert await anext(stream) == "Two"
    with pytest.raises(LLMError, match="unexpectedly stopped"):
        await anext(stream)


async def test_a_refused_request_is_an_error_before_any_piece(daemon) -> None:
    daemon([], status=404)

    with pytest.raises(LLMError, match="Ollama call failed"):
        await _pieces(OllamaProvider("http://test", model="not-pulled"))


async def test_a_line_that_is_not_json_is_an_error(daemon) -> None:
    daemon(["<html>a proxy's error page</html>"])

    with pytest.raises(LLMError, match="Ollama call failed"):
        await _pieces(OllamaProvider("http://test", model="some-model"))


async def test_a_hosted_model_streams_and_is_sent_no_cap(daemon) -> None:
    fake = daemon(ANSWER, thinks=True)

    pieces = await _pieces(OllamaCloudProvider("http://test", model="glm-5.3-flash:cloud"))

    assert "".join(pieces) == "Two are waiting."
    [body] = fake.streamed
    assert "num_ctx" not in body["options"]
    assert "think" not in body
    [entry] = audit.read_trail()
    assert entry.left_machine, "the address is localhost and the model is not"


async def test_a_provider_that_cannot_stream_sends_its_answer_whole() -> None:
    """Gemini, Anthropic, OpenRouter and the stub have no `stream` yet."""
    stub = StubProvider()
    assert not hasattr(stub, "stream")

    pieces = [piece async for piece in stream_text(stub, "sys", "usr", max_tokens=50)]

    assert pieces == [await stub.complete("sys", "usr", max_tokens=50)]


async def test_a_provider_that_can_stream_is_streamed(daemon) -> None:
    daemon(ANSWER)
    local = OllamaProvider("http://test", model="some-model")

    pieces = [piece async for piece in stream_text(local, "sys", "usr", max_tokens=600)]

    assert pieces == ["Two", " are", " waiting."]


#: The closing line as the daemon sends it: no text, and what the call cost.
CLOSING = json.dumps(
    {
        "message": {"content": ""},
        "done": True,
        "done_reason": "stop",
        "total_duration": 17_400_000_000,
        "load_duration": 120_000_000,
        "prompt_eval_count": 1_312,
        "prompt_eval_duration": 5_050_000_000,
        "eval_count": 146,
        "eval_duration": 12_100_000_000,
    }
)


async def test_a_streamed_answer_is_measured_like_a_plain_one(daemon) -> None:
    """Every answer in the dock is streamed. The stream and the timings were
    written apart, so once both had merged the assistant's calls were the one
    kind that kept no numbers. The closing line carries the same ones."""
    daemon([_line("Two"), _line(" are waiting."), CLOSING])

    await _pieces(OllamaProvider("http://test", model="some-model"))

    [kept] = timing.read_timings()
    assert (kept.prompt_tokens, kept.answer_tokens) == (1_312, 146)
    assert (kept.prompt_ms, kept.answer_ms) == (5_050.0, 12_100.0)
    assert kept.context_limit == get_settings().ollama_num_ctx
    assert kept.user_sha256 == audit.digest_of("usr")


async def test_a_stream_closed_early_has_nothing_to_measure(daemon) -> None:
    """The numbers are on the last line, and a reader who left never read it."""
    daemon([_line("Two"), _line(" are waiting."), CLOSING])
    stream = OllamaProvider("http://test", model="some-model").stream("sys", "usr")

    await anext(stream)
    await stream.aclose()

    assert timing.read_timings() == []


async def test_an_error_line_is_quoted_no_further_than_a_reason_runs(daemon) -> None:
    daemon([json.dumps({"error": "runner stopped: " + "the prompt, repeated back " * 60})])

    with pytest.raises(LLMError) as failed:
        await _pieces(OllamaProvider("http://test", model="some-model"))

    assert "runner stopped" in str(failed.value)
    assert len(str(failed.value)) < 400
