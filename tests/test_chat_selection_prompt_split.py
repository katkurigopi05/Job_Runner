"""The answer prompt carries no selection protocol, and a bare selection is never shown.

On 2026-11-02 the owner asked "Have I had any replies, and what did they say?"
and a skills-gap question on Kaggle's Qwen models, and both answers were the
text `{"ids": []}`. One system prompt carried the answer instructions and the
evidence-selection protocol together, and said the protocol "takes precedence
over the answer-format instructions below": a model answering an ordinary
question followed it. The dock then sent those answers back as history, which
taught the next model the same.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from apps.api import chat_history
from apps.api.chat_selection import is_bare_selection
from apps.api.errors import ApiError
from apps.api.routers import chat, chat_stream
from packages.core.schemas import ChatRequest, ChatTurn
from packages.llm.prompts import CHAT_SELECTION_SYSTEM, CHAT_SYSTEM
from packages.matching.retrieve import Retrieval
from tests.test_chat_selection_routes import _ready


def test_the_answer_prompt_carries_no_selection_protocol():
    assert "EVIDENCE_SELECTION" not in CHAT_SYSTEM.text
    assert '{"ids"' not in CHAT_SYSTEM.text
    assert "EVIDENCE_SELECTION" in CHAT_SELECTION_SYSTEM.text
    assert '{"ids": []}' in CHAT_SELECTION_SYSTEM.text


@pytest.mark.parametrize("text", ['{"ids": []}', ' {"ids":["P1"]} \n', '```json\n{"ids": []}\n```'])
def test_a_bare_selection_is_recognised(text):
    assert is_bare_selection(text)


@pytest.mark.parametrize(
    "text", ["No replies yet.", '{"ids": [], "note": "x"}', 'You have {"ids": []} here', ""]
)
def test_an_answer_is_not_a_bare_selection(text):
    assert not is_bare_selection(text)


def _provider(raw: str):
    return type("Provider", (), {"model": "test", "complete": AsyncMock(return_value=raw)})()


def _ordinary_ready():
    return chat.Ready(
        selected="kaggle",
        include_mail=False,
        prompt="CONTEXT:\nreplies: none\n\nQUESTION:\nAny replies?",
        found=Retrieval(attempted=False),
    )


@pytest.mark.parametrize("route", ["plain", "stream"])
async def test_each_path_is_sent_its_own_prompt(monkeypatch, route):
    for ready, raw, system in (
        (_ordinary_ready(), "No replies yet.", CHAT_SYSTEM.text),
        (_ready(), '{"ids":["P1"]}', CHAT_SELECTION_SYSTEM.text),
    ):
        prepared = AsyncMock(return_value=ready)
        monkeypatch.setattr(chat, "prepare", prepared)
        monkeypatch.setattr(chat_stream, "_prepared", prepared)
        provider = _provider(raw)
        monkeypatch.setattr(chat.llm_router, "build_provider", lambda name, p=provider: p)
        body = ChatRequest(message="Any replies?", provider="kaggle")
        if route == "plain":
            await chat.chat(body, None)
        else:
            response = await chat_stream.chat_stream(body)
            [part async for part in response.body_iterator]
        assert provider.complete.call_args.args[0] == system


async def test_a_bare_selection_as_an_answer_is_refused_on_the_plain_route(monkeypatch):
    monkeypatch.setattr(chat, "prepare", AsyncMock(return_value=_ordinary_ready()))
    monkeypatch.setattr(chat.llm_router, "build_provider", lambda name: _provider('{"ids": []}'))

    with pytest.raises(ApiError) as raised:
        await chat.chat(ChatRequest(message="Any replies?", provider="kaggle"), None)

    assert "selection" in str(raised.value.message).lower()


async def test_a_bare_selection_is_rejected_before_the_stream_starts(monkeypatch):
    monkeypatch.setattr(chat_stream, "_prepared", AsyncMock(return_value=_ordinary_ready()))
    monkeypatch.setattr(chat.llm_router, "build_provider", lambda name: _provider('{"ids": []}'))

    with pytest.raises(ApiError, match="selection data instead of an answer"):
        await chat_stream.chat_stream(ChatRequest(message="Any replies?", provider="kaggle"))


def test_an_earlier_bare_selection_is_not_remembered():
    turns = [
        ChatTurn(question="Any replies?", answer='{"ids": []}', mail_in_context=False),
        ChatTurn(question="What needs me?", answer="Two need review.", mail_in_context=False),
    ]

    remembered = chat_history.remember(turns, question="And the skills?", mail_may_go=False)

    assert [turn.answer for turn in remembered.turns] == ["Two need review."]
