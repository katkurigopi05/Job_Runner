"""Opt-in packing preserves facts and removes only entire posting excerpts."""

import pytest

from apps.api.chat_budget import BudgetExceeded, PromptBudget, pack


def words(system, prompt):
    return len((system + " " + prompt).split())


def render(labels):
    return "COUNTS: 12. QUESTION: Kafka? " + " ".join(
        f"[{label}] metadata evidence " + ("long excerpt " * 10 if label in labels else "withheld")
        for label in ("P1", "P2")
    )


def test_fitting_context_is_byte_identical_and_reserves_answer_and_safety():
    budget = PromptBudget(counter=words, tokenizer_id="test-words", context_tokens=100)
    prompt, report = pack(
        system="system",
        render=render,
        labels=["P1", "P2"],
        budget=budget,
        answer_tokens=10,
        safety_tokens=5,
    )
    assert prompt == render(frozenset(["P1", "P2"]))
    assert report.removed_excerpts == []
    assert report.prompt_tokens + report.answer_tokens + report.safety_tokens <= 100


def test_overflow_removes_lowest_ranked_whole_excerpt_not_its_metadata_or_evidence():
    budget = PromptBudget(counter=words, tokenizer_id="test-words", context_tokens=50)
    prompt, report = pack(
        system="system",
        render=render,
        labels=["P1", "P2"],
        budget=budget,
        answer_tokens=10,
        safety_tokens=5,
    )
    assert report.removed_excerpts == ["P2"]
    assert prompt == render(frozenset(["P1"]))
    assert "COUNTS: 12. QUESTION: Kafka?" in prompt
    assert "[P2] metadata evidence withheld" in prompt


def test_mandatory_context_too_large_refuses_without_a_model_call():
    with pytest.raises(BudgetExceeded):
        pack(
            system="system",
            render=render,
            labels=["P1", "P2"],
            budget=PromptBudget(counter=words, tokenizer_id="test", context_tokens=10),
            answer_tokens=5,
            safety_tokens=2,
        )


@pytest.mark.parametrize("count", [-1, True, 1.2])
def test_invalid_token_count_cannot_look_like_a_fitting_prompt(count):
    with pytest.raises(ValueError, match="token counter"):
        pack(
            system="s",
            render=render,
            labels=["P1", "P2"],
            budget=PromptBudget(
                counter=lambda s, p: count, tokenizer_id="test", context_tokens=10000
            ),
        )


async def test_actual_prepare_preserves_default_and_facts_when_packed(worker_session):
    from apps.api.routers import chat
    from packages.core.schemas import ChatRequest, ChatTurn
    from packages.llm.provider import StubProvider
    from tests.test_chat_skill_gaps import _owner, _requirements, _target

    profile, company = await _owner(worker_session)
    posting = await _target(
        worker_session,
        profile,
        company,
        "Data Engineer",
        "Kafka streams. " * 200,
        _requirements(preferred=["kafka"]),
    )
    await worker_session.commit()
    body = ChatRequest(
        message="jobs using kafka",
        history=[
            ChatTurn(
                question="Which streaming roles did we discuss?",
                answer="Untrusted earlier answer.",
                mail_in_context=False,
                cited=[posting.id],
            )
        ],
    )
    ready = await chat.prepare(body, worker_session)
    assert isinstance(ready, chat.Ready)
    high = await chat.prepare(
        body,
        worker_session,
        context_budget=PromptBudget(counter=words, tokenizer_id="test-words", context_tokens=10000),
    )
    assert isinstance(high, chat.Ready)
    assert high.prompt == ready.prompt
    assert high.budget is not None
    assert ready.budget is None
    mandatory = ready.prompt.split("       excerpt:")[0]
    small = await chat.prepare(
        body,
        worker_session,
        context_budget=PromptBudget(
            counter=words,
            tokenizer_id="test-words",
            context_tokens=words(chat.SYSTEM, ready.prompt) + chat.ANSWER_TOKENS + 128 - 10,
        ),
    )
    assert isinstance(small, chat.Ready)
    assert small.found == ready.found
    assert small.remembered == ready.remembered
    assert small.budget.removed_excerpts == ["P1"]
    assert mandatory in small.prompt
    assert "preferred" in small.prompt.lower()
    assert '"question": "jobs using kafka"' in small.prompt
    assert '"evidence": []' in small.prompt
    assert small.selection_evidence == ()
    assert "excerpt: withheld to fit the context budget" in small.prompt
    assert "Which streaming roles did we discuss?" not in small.prompt
    assert [
        line for line in small.prompt.splitlines() if not line.startswith(("       excerpt:", "{"))
    ] == [
        line for line in ready.prompt.splitlines() if not line.startswith(("       excerpt:", "{"))
    ]
    answer = await StubProvider().complete(chat.SYSTEM, small.prompt)
    assert (
        chat.reply_for(small, "stub", answer).sources
        == chat.reply_for(ready, "stub", answer).sources
    )
