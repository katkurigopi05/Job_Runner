"""Posting answers validate the entire model output before either route displays facts."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import pytest

from apps.api.routers import chat, chat_stream
from packages.core.schemas import ChatRequest
from packages.matching.retrieve import Passage, Retrieval


def _ready():
    from apps.api.chat_selection import EvidenceItem

    passage = Passage(
        "P1",
        uuid.uuid4(),
        "Engineer",
        "Example Labs",
        "San Francisco, CA",
        "https://example.test/job",
        "Build LLM orchestration tools.",
        None,
    )
    return chat.Ready(
        selected="kaggle",
        include_mail=False,
        prompt="selection prompt",
        found=Retrieval(attempted=True, searched=75, passages=(passage,)),
        selection_evidence=(EvidenceItem(id="P1", text=passage.excerpt, source_label="P1"),),
    )


@pytest.mark.parametrize("route", ["plain", "stream"])
@pytest.mark.parametrize(
    "raw",
    [
        '{"ids":["P1"]}',
        '{"ids":["P999"]}',
        "75 jobs ground LLM answers on company documents.",
        '{"ids":[]}',
    ],
)
async def test_both_routes_only_display_supplied_evidence(monkeypatch, route, raw):
    ready = _ready()
    prepared = AsyncMock(return_value=ready)
    monkeypatch.setattr(chat, "prepare", prepared)
    monkeypatch.setattr(chat_stream, "_prepared", prepared)
    provider = type("Provider", (), {"model": "test", "complete": AsyncMock(return_value=raw)})()
    monkeypatch.setattr(chat.llm_router, "build_provider", lambda name: provider)
    body = ChatRequest(
        message="Which jobs ground LLM answers on company documents?", provider="kaggle"
    )
    if route == "plain":
        result = await chat.chat(body, None)
    else:
        response = await chat_stream.chat_stream(body)
        frames = [json.loads(part) async for part in response.body_iterator]
        assert [part["type"] for part in frames] == ["done"]
        from packages.core.schemas import ChatReply

        result = ChatReply.model_validate(frames[0]["reply"])
    assert "Build LLM orchestration tools." in result.reply
    assert "75 jobs ground" not in result.reply
    assert "P999" not in result.reply
    assert "Searched 75" in result.reply
    assert "total matching count is unknown" in result.reply
    assert result.unsupported_citations == []
    provider.complete.assert_awaited_once()
    assert provider.complete.call_args.args[0] == chat.SYSTEM


@pytest.mark.parametrize("route", ["/chat", "/chat/stream"])
async def test_real_preparation_uses_selection_and_omits_private_context(
    client, worker_session, monkeypatch, route
):
    from tests.test_chat_title_words import _company, _posting

    company = await _company(worker_session)
    posting = await _posting(worker_session, company, "Engineer", "Build LLM orchestration tools.")
    posting.location = "San Francisco, CA"
    await worker_session.commit()
    provider = type(
        "Provider", (), {"model": "test", "complete": AsyncMock(return_value='{"ids":["P1"]}')}
    )()
    monkeypatch.setattr(chat.llm_router, "build_provider", lambda name: provider)
    response = await client.post(
        route, json={"message": "Which jobs use LLM orchestration?", "provider": "kaggle"}
    )
    assert response.status_code == 200
    reply = (
        json.loads(response.text.splitlines()[-1])["reply"]
        if route.endswith("stream")
        else response.json()
    )
    assert "Build LLM orchestration tools." in reply["reply"]
    prompt = provider.complete.call_args.args[1]
    assert prompt.startswith("EVIDENCE_SELECTION\n")
    assert "APPLICATIONS BY STATUS" not in prompt
    assert "WAITING ON YOU" not in prompt
    assert reply["shared_mail"] is False
    assert reply["mail_in_context"] is False


async def test_packed_selection_never_accepts_omitted_evidence(worker_session, monkeypatch):
    from apps.api.chat_budget import PromptBudget
    from tests.test_chat_title_words import _company, _posting

    company = await _company(worker_session)
    post = await _posting(
        worker_session, company, "Engineer", "Zetastack OMITTED_SOURCE_TEXT. " * 200
    )
    post.location = "San Francisco, CA"
    await worker_session.commit()
    body = ChatRequest(message="jobs using Zetastack", provider="kaggle")
    ready = await chat.prepare(body, worker_session)
    assert isinstance(ready, chat.Ready)
    budget = PromptBudget(
        counter=lambda system, user: len(user),
        tokenizer_id="test-chars",
        context_tokens=len(ready.prompt) + chat.ANSWER_TOKENS + 128 - 50,
    )
    packed = await chat.prepare(body, worker_session, context_budget=budget)
    assert isinstance(packed, chat.Ready)
    assert packed.budget.removed_excerpts == ["P1"]
    assert packed.selection_evidence == ()
    assert '"id": "P1"' not in packed.prompt
    assert packed.budget.prompt_tokens == len(packed.prompt)
    assert packed.factual_answer is not None
    assert "OMITTED_SOURCE_TEXT" not in packed.factual_answer


async def test_sponsorship_summary_never_repeats_a_removed_raw_excerpt(worker_session, monkeypatch):
    from dataclasses import replace

    from apps.api.chat_budget import PromptBudget
    from packages.matching.sponsored import SponsorshipCount

    ready = _ready()
    passage = replace(ready.found.passages[0], excerpt="SENTINEL_RAW_SOURCE " * 100)
    found = replace(
        ready.found,
        passages=(passage,),
        sponsorship=SponsorshipCount(1, 0, 0, "retrieved postings"),
    )
    monkeypatch.setattr(chat, "retrieve", AsyncMock(return_value=found))
    body = ChatRequest(message="Which jobs offer visa sponsorship?", provider="kaggle")
    full = await chat.prepare(body, worker_session)
    assert isinstance(full, chat.Ready)
    assert "SENTINEL_RAW_SOURCE" not in full.selection_summary
    small = await chat.prepare(
        body,
        worker_session,
        context_budget=PromptBudget(
            counter=lambda system, user: len(user),
            tokenizer_id="test-chars",
            context_tokens=len(full.prompt) + chat.ANSWER_TOKENS + 128 - 100,
        ),
    )
    assert isinstance(small, chat.Ready)
    assert small.selection_evidence == ()
    assert "SENTINEL_RAW_SOURCE" not in small.factual_answer
