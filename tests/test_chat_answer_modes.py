"""Mode-specific prompts and guards for the captured prose-format failure."""

from unittest.mock import AsyncMock

import pytest

from apps.api.routers import chat, chat_stream
from packages.core.schemas import ChatRequest
from packages.llm.provider import LLMError
from packages.matching.retrieve import Retrieval


@pytest.mark.parametrize("raw", ['{"ids": []}', '  {"ids":["P1"]}\n', '```json\n{"ids":[]}\n```'])
async def test_selection_json_is_never_shown_as_a_prose_answer(monkeypatch, raw):
    ready = chat.Ready(
        selected="kaggle",
        include_mail=False,
        prompt="CONTEXT: no mail\nQUESTION: did they reply?",
        found=Retrieval(attempted=False),
    )
    monkeypatch.setattr(chat, "prepare", AsyncMock(return_value=ready))
    monkeypatch.setattr(chat_stream, "_prepared", AsyncMock(return_value=ready))
    provider = type("Provider", (), {"model": "test", "complete": AsyncMock(return_value=raw)})()
    monkeypatch.setattr(chat.llm_router, "build_provider", lambda _: provider)
    body = ChatRequest(message="did the hiring manager reply?", provider="kaggle")
    from apps.api.errors import ApiError

    with pytest.raises(ApiError, match="selection data instead of an answer"):
        await chat.chat(body, None)
    with pytest.raises(ApiError, match="selection data instead of an answer"):
        await chat_stream.chat_stream(body)
    assert all(call.args[0] == chat.SYSTEM for call in provider.complete.call_args_list)
    assert "EVIDENCE_SELECTION" not in chat.SYSTEM
    assert chat.SELECTION_SYSTEM != chat.SYSTEM


async def test_fragmented_protocol_is_rejected_before_any_stream_delta():
    closed = []

    async def pieces():
        try:
            for part in [" ", "\n", "{", '"ids"', ":", "[]", "}"]:
                yield part
        finally:
            closed.append(True)

    exposed = []
    with pytest.raises(LLMError):
        async for part in chat_stream._prose_pieces(pieces()):
            exposed.append(part)
    assert exposed == [] and closed == [True]


async def test_guard_preserves_legitimate_structured_answers():
    async def pieces():
        yield '{"status":'
        yield '"unknown"}'

    assert "".join([p async for p in chat_stream._prose_pieces(pieces())]) == '{"status":"unknown"}'


async def test_plain_prose_keeps_incremental_delivery():
    seen = []

    async def pieces():
        seen.append("first")
        yield "No mail was supplied."
        seen.append("second")
        yield " I cannot determine the reply status."

    stream = chat_stream._prose_pieces(pieces())
    assert await anext(stream) == "No mail was supplied."
    assert seen == ["first"]
    await stream.aclose()
