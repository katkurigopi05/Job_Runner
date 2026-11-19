"""Optional prose is buffered, source-bound, and uses the same provider."""

from __future__ import annotations

from dataclasses import replace

import pytest

from apps.api.routers import chat
from packages.core.config import Settings
from packages.llm.provider import LLMError, StubProvider
from tests.test_chat_selection_routes import _ready


class ProseStub(StubProvider):
    model = "test-27b"

    def __init__(self, prose):
        super().__init__({"selection prompt": '{"ids":["P1"]}', "EVIDENCE_PROSE": prose})


@pytest.mark.parametrize("provider", ["kaggle", "ollama", "openrouter"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_flag_and_provider_gate(monkeypatch, provider, enabled):
    assert "chat_kaggle_prose" in Settings.model_fields
    stub = ProseStub("Build LLM orchestration tools. [P1]")
    ready = replace(_ready(), selected=provider, selection_provider=stub)
    monkeypatch.setenv("CHAT_KAGGLE_PROSE", str(enabled).lower())
    chat.get_settings.cache_clear()
    try:
        reply = await chat.selected_reply(ready)
        assert len(stub.calls) == (2 if enabled and provider == "kaggle" else 1)
        if enabled and provider == "kaggle":
            assert "Build LLM orchestration tools. [P1]" in reply.reply
            assert "Source text" not in reply.reply
    finally:
        chat.get_settings.cache_clear()


@pytest.mark.parametrize(
    "prose",
    [
        "Build LLM orchestration tools. [P999]",
        "Build 999 LLM orchestration tools. [P1]",
        "Build ninety-nine LLM orchestration tools. [P1]",
        "Invented Corp is hiring. [P1]",
        "Chief Wizard Engineer is hiring. [P1]",
        "Build LLM orchestration tools.",
        "Build LLM orchestration tools. [P1] " * 7,
        "",
        "Engineer works at orchestration tools. [P1]",
    ],
)
async def test_invalid_prose_falls_back_without_echo(monkeypatch, prose):
    stub = ProseStub(prose)
    ready = replace(_ready(), selection_provider=stub)
    monkeypatch.setenv("CHAT_KAGGLE_PROSE", "true")
    chat.get_settings.cache_clear()
    try:
        expected = chat.selection_reply_for(ready, stub.model, '{"ids":["P1"]}')
        result = await chat.selected_reply(ready)
        assert len(stub.calls) == 2
        assert result.reply.startswith(expected.reply)
        assert result.reply.endswith(
            "Prose could not be verified; showing the source-based answer."
        )
        assert result.sources == expected.sources
    finally:
        chat.get_settings.cache_clear()


async def test_unselected_sources_are_not_sent_or_cited(monkeypatch):
    from apps.api.chat_selection import EvidenceItem

    stub = ProseStub("UNSELECTED material. [P2]")
    ready = replace(
        _ready(),
        selection_provider=stub,
        selection_evidence=(
            *_ready().selection_evidence,
            EvidenceItem(id="P2", text="UNSELECTED material.", source_label="P2"),
        ),
    )
    monkeypatch.setenv("CHAT_KAGGLE_PROSE", "true")
    chat.get_settings.cache_clear()
    try:
        result = await chat.selected_reply(ready)
        assert len(stub.calls) == 2
        assert "UNSELECTED" not in stub.calls[1][1]
        assert result.reply.endswith(
            "Prose could not be verified; showing the source-based answer."
        )
    finally:
        chat.get_settings.cache_clear()


async def test_provider_failure_falls_back(monkeypatch):
    class Failing(ProseStub):
        async def complete(self, system, user, **kwargs):
            if "EVIDENCE_PROSE" in user:
                raise LLMError("sentinel private error")
            return await super().complete(system, user, **kwargs)

    stub = Failing("")
    monkeypatch.setenv("CHAT_KAGGLE_PROSE", "true")
    chat.get_settings.cache_clear()
    try:
        result = await chat.selected_reply(replace(_ready(), selection_provider=stub))
        assert "sentinel" not in result.reply
        assert "Prose could not be verified" in result.reply
    finally:
        chat.get_settings.cache_clear()


def test_prose_flag_defaults_off():
    assert Settings(_env_file=None).chat_kaggle_prose is False


async def test_graph_prose_uses_anonymous_sources(monkeypatch):
    from apps.api.chat_selection import EvidenceItem
    from packages.matching.graph_rag import GraphEvidence, GraphResult

    ready = _ready()
    graph = GraphResult(
        found=ready.found,
        summary="1 job reviewed.",
        evidence=(
            GraphEvidence("P1", "PRIVATE_REPO source text.", "Public project declares Python."),
        ),
        answer_summary="Graph answer.",
    )
    stub = ProseStub("Public project declares Python. [P1]")
    ready = replace(
        ready,
        graph_result=graph,
        selection_evidence=(
            EvidenceItem(id="P1", text="PRIVATE_REPO source text.", source_label="P1"),
        ),
        selection_provider=stub,
    )
    monkeypatch.setenv("CHAT_KAGGLE_PROSE", "true")
    chat.get_settings.cache_clear()
    try:
        reply = await chat.selected_reply(ready)
        assert len(stub.calls) == 2
        assert "PRIVATE_REPO" not in stub.calls[1][1]
        assert "Public project declares Python" in stub.calls[1][1]
        assert "Public project declares Python" in reply.reply
    finally:
        chat.get_settings.cache_clear()


async def test_prose_budget_never_discards_selected_evidence(monkeypatch):
    from apps.api.chat_budget import PromptBudget

    stub = ProseStub("Build LLM orchestration tools. [P1]")
    ready = replace(
        _ready(),
        selection_provider=stub,
        prose_budget=PromptBudget(
            counter=lambda system, user: len(system + user),
            tokenizer_id="test-chars",
            context_tokens=750,
        ),
    )
    monkeypatch.setenv("CHAT_KAGGLE_PROSE", "true")
    chat.get_settings.cache_clear()
    try:
        reply = await chat.selected_reply(ready)
        assert len(stub.calls) == 1
        assert "Prose could not be verified" in reply.reply
    finally:
        chat.get_settings.cache_clear()


async def test_two_calls_keep_privacy_and_audit(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    from packages.llm import audit, privacy
    from packages.llm.privacy import protect_provider

    monkeypatch.setattr(audit, "audit_path", lambda: tmp_path / "audit.jsonl")
    stub = ProseStub("Build LLM orchestration tools. [P1]")
    stub.name = "kaggle"
    monkeypatch.setattr(privacy, "load_identifiers", AsyncMock(return_value=privacy.Identifiers()))
    wrapped = protect_provider(stub)
    ready = replace(
        _ready(),
        selection_provider=wrapped,
        selection_summary="Contact private-person@example.test; 75 postings searched.",
    )
    monkeypatch.setenv("CHAT_KAGGLE_PROSE", "true")
    chat.get_settings.cache_clear()
    try:
        await chat.selected_reply(ready)
        assert len(stub.calls) == 2
        assert "private-person@example.test" not in stub.calls[1][1]
        assert privacy.load_identifiers.await_count == 2
        entries = audit.read_trail()
        assert [e.prompt_name for e in entries] == ["assistant.selection", "assistant.prose"]
    finally:
        chat.get_settings.cache_clear()


@pytest.mark.parametrize("route", ["plain", "stream"])
async def test_valid_prose_is_validated_before_either_route_displays(monkeypatch, route):
    import json
    from unittest.mock import AsyncMock

    from apps.api.routers import chat_stream
    from packages.core.schemas import ChatRequest

    stub = ProseStub("Build LLM orchestration tools. [P1]")
    ready = replace(_ready(), selection_provider=stub)
    prepared = AsyncMock(return_value=ready)
    monkeypatch.setattr(chat, "prepare", prepared)
    monkeypatch.setattr(chat_stream, "_prepared", prepared)
    monkeypatch.setenv("CHAT_KAGGLE_PROSE", "true")
    chat.get_settings.cache_clear()
    try:
        request = ChatRequest(message="Which jobs use LLM orchestration?", provider="kaggle")
        if route == "plain":
            reply = await chat.chat(request, None)
        else:
            response = await chat_stream.chat_stream(request)
            frames = [json.loads(frame) async for frame in response.body_iterator]
            assert [f["type"] for f in frames] == ["done"]
            from packages.core.schemas import ChatReply

            reply = ChatReply.model_validate(frames[0]["reply"])
        assert len(stub.calls) == 2
        assert reply.reply.startswith("Build LLM orchestration tools. [P1]")
        assert reply.sources[0].cited
    finally:
        chat.get_settings.cache_clear()


@pytest.mark.parametrize(
    "source,prose",
    [
        ("We do not require Python.", "Require Python. [P1]"),
        ("We are not hiring at Acme.", "Hiring at Acme. [P1]"),
        ("We pay $50 an hour.", "We pay £50 an hour. [P1]"),
    ],
)
def test_prose_cannot_remove_negation_or_change_units(source, prose):
    from apps.api.chat_prose import validate_prose
    from apps.api.chat_selection import EvidenceItem

    assert not validate_prose(prose, [EvidenceItem(id="P1", source_label="P1", text=source)], "")


def test_line_wrapping_cannot_remove_negation():
    from apps.api.chat_prose import validate_prose
    from apps.api.chat_selection import EvidenceItem

    evidence = [EvidenceItem(id="P1", source_label="P1", text="We are not\nhiring at Acme.")]
    assert not validate_prose("Hiring at Acme. [P1]", evidence, "")
