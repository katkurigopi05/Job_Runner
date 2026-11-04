"""Recorded requirement labels and exact title matches do not need model judgment."""

import uuid
from unittest.mock import AsyncMock

import pytest

from apps.api.routers import chat
from apps.api.routers.chat_evidence import compound_required_evidence
from packages.core.schemas import ChatRequest
from packages.matching.retrieve import Passage, Retrieval


def sample():
    passages = tuple(
        Passage(
            f"P{i}",
            uuid.uuid4(),
            "Engineer",
            "Example",
            "SF, CA",
            "https://example.test",
            text,
            None,
        )
        for i, text in enumerate(
            ["Stack: Kafka and Python", "Kafka or Kinesis", "Python required", "Python preferred"],
            1,
        )
    )

    def req(group, key, quote):
        return {"skills": {group: [{"skill": key, "label": key, "quote": quote}]}}

    return Retrieval(attempted=True, searched=4, passages=passages), {
        passages[0].posting_id: req("unclassified", "python", "Stack: Kafka and Python"),
        passages[1].posting_id: req("required", "kafka", "Kafka or Kinesis"),
        passages[2].posting_id: req("required", "python", "Proficiency in Python"),
        passages[3].posting_id: req("preferred", "python", "Python preferred"),
    }


def test_compound_or_uses_only_direct_requirements():
    found, requirements = sample()
    result = compound_required_evidence("Which jobs require Kafka or Python?", found, requirements)
    assert result is not None and result.ids == ("P3",)
    assert "required" in result.items[0].text
    assert "Proficiency in Python" in result.items[0].text


def test_compound_and_requires_both_and_preserves_unknown():
    found, requirements = sample()
    result = compound_required_evidence("Which jobs require Kafka and Python?", found, requirements)
    assert result is not None and result.ids == ()
    assert (
        compound_required_evidence(
            "Which jobs do not require Kafka or Python?", found, requirements
        )
        is None
    )
    assert (
        compound_required_evidence("Which jobs mention Kafka or Python?", found, requirements)
        is None
    )
    assert (
        compound_required_evidence(
            "Which jobs require Kafka and Python or Go?", found, requirements
        )
        is None
    )


async def test_compound_requirement_preparation_is_model_free(worker_session, monkeypatch):
    found, requirements = sample()
    monkeypatch.setattr(chat, "retrieve", AsyncMock(return_value=found))
    monkeypatch.setattr(chat, "_requirements_of", AsyncMock(return_value=requirements))
    ready = await chat.prepare(
        ChatRequest(message="Which jobs require Kafka or Python?", provider="kaggle"),
        worker_session,
    )
    assert isinstance(ready, chat.Ready)
    assert ready.factual_answer is not None and ready.selection_evidence is None
    assert "[P3]" in ready.factual_answer
    assert "[P1]" not in ready.factual_answer and "[P2]" not in ready.factual_answer
    assert "exhaustive" in ready.factual_answer
    reply = chat.fixed_reply(ready)
    assert reply.provider == "facts" and reply.local and reply.model is None


async def test_title_only_ai_posting_is_returned_without_a_model(
    client, worker_session, monkeypatch
):
    from tests.test_chat_title_words import _company, _posting

    company = await _company(worker_session)
    posting = await _posting(worker_session, company, "AI Engineer", "")
    posting.location = "San Francisco, CA"
    await worker_session.commit()
    model = AsyncMock(side_effect=AssertionError("Title matches must not call a model"))
    monkeypatch.setattr(chat.llm_router, "build_provider", model)
    response = await client.post(
        "/chat", json={"message": "any jobs with job types AI engineer", "provider": "kaggle"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "facts" and data["model"] is None
    assert any(source["posting_id"] == str(posting.id) for source in data["sources"])
    model.assert_not_called()


def test_compound_shortcut_preserves_extra_task_constraints():
    found, requirements = sample()
    for question in (
        "Which jobs require Kafka or Python for customer-support agents?",
        "Which jobs require Kafka or Python and build customer-support agents?",
        "Which remote jobs require Kafka or Python?",
    ):
        assert compound_required_evidence(question, found, requirements) is None


@pytest.mark.parametrize(
    "question, application_id",
    [
        ("Which of my applications for AI engineer jobs are waiting?", None),
        ("AI engineer jobs", uuid.uuid4()),
    ],
)
async def test_title_shortcut_preserves_application_context(
    worker_session, monkeypatch, question, application_id
):
    from types import SimpleNamespace

    found = Retrieval(attempted=True, roles=("AI / ML engineer",), role_total=1)
    monkeypatch.setattr(chat, "retrieve", AsyncMock(return_value=found))
    monkeypatch.setattr(
        chat,
        "_read_context",
        AsyncMock(return_value=SimpleNamespace(context="Application context")),
    )
    ready = await chat.prepare(
        ChatRequest(message=question, provider="kaggle", application_id=application_id),
        worker_session,
    )
    assert ready.factual_answer is None
    assert ready.selection_evidence is None
    assert "Application context" in ready.prompt
