"""What the assistant's reply can and cannot say for itself.

`grounded` is true for every answer a model wrote. That says the model was
handed the owner's data to answer from. It was read as more: nothing checks
the answer against that data, and a reply saying "12 of 11,147 postings
require Go" carried `grounded: true` on 2026-10-10 over a table counted on the
top 100 matches.

So the reply says the things that can be known without judging the prose:

- a posting the answer cites that was never handed to the model;
- how many postings each number in the answer could be about, each under a
  name of its own, so that five shown is never read as five found;
- for a skill the question named, how each posting asks for it, with the line.

None of it is a verdict on the answer. A citation that exists is not a claim
that is supported.
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient

from packages.core.models import Application, Posting
from packages.core.schemas import ChatReply
from packages.llm.provider import StubProvider
from tests.test_chat_skill_gaps import _owner, _requirements, _target


def _answers(monkeypatch, text: str) -> dict[str, str]:
    """A model that says `text`, and what it was handed."""
    import apps.api.routers.chat as chat_module

    seen: dict[str, str] = {}

    class Says(StubProvider):
        async def complete(self, system, user, *, max_tokens=1024, temperature=0.2) -> str:
            seen["system"], seen["user"] = system, user
            return text

    monkeypatch.setattr(chat_module.llm_router, "build_provider", lambda name=None: Says())
    return seen


async def _kafka_postings(session) -> dict[str, Posting]:
    profile, company = await _owner(session)

    def line(text: str) -> dict:
        return {"skill": "kafka", "label": "Kafka", "quote": text}

    posts = {
        "required": await _target(
            session,
            profile,
            company,
            "Stream Engineer",
            "Requirements: 5+ years running Kafka in production.",
            {"skills": {"required": [line("5+ years running Kafka in production")]}},
        ),
        "either": await _target(
            session,
            profile,
            company,
            "Platform Engineer",
            "Requirements: Experience with Kafka or Kinesis.",
            {"skills": {"required": [line("Experience with Kafka or Kinesis")]}},
        ),
        "named": await _target(
            session,
            profile,
            company,
            "Backend Engineer",
            "We moved our event bus off Kafka last year.",
            {"skills": {"unclassified": [line("We moved our event bus off Kafka last year")]}},
        ),
    }
    await session.commit()
    return posts


# --------------------------------------------------------------------------
# Citations
# --------------------------------------------------------------------------


async def test_model_prose_with_an_unknown_citation_is_rejected(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    """Three were handed over. "[P7]" is a source the model made up."""
    await _kafka_postings(worker_session)
    _answers(monkeypatch, "Stream Engineer needs it [P1], and so does another [P7].")

    body = (await client.post("/chat", json={"message": "Which jobs use Kafka?"})).json()

    assert body["unsupported_citations"] == []
    assert "P7" not in body["reply"]
    assert "Stream Engineer needs it" not in body["reply"]
    assert {s["label"] for s in body["sources"] if s["cited"]} == {"P1", "P2", "P3"}


async def test_citing_only_what_was_supplied_reports_nothing(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    await _kafka_postings(worker_session)
    _answers(monkeypatch, "Two of them: [P1] and [P2].")

    body = (await client.post("/chat", json={"message": "Which jobs use Kafka?"})).json()

    assert body["unsupported_citations"] == []


async def test_a_citation_where_nothing_was_searched_is_one_too(
    client: AsyncClient, monkeypatch
) -> None:
    """No posting was handed over at all, so any label is invented."""
    _answers(monkeypatch, "One is waiting [P1].")

    body = (await client.post("/chat", json={"message": "hello"})).json()

    assert body["sources"] == []
    assert body["unsupported_citations"] == ["P1"]


async def test_a_posting_read_again_from_an_earlier_answer_was_supplied(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    """The conversation carries the postings its earlier answers cited, read
    again and labelled after the ones found now (`chat_history`). Citing one
    of those is citing something the model was given.

    The check and the memory were written apart and met in one merge. The
    check knew only the postings found for this question, so "the one from
    before [P1]" was reported, and shown under the answer, as a source the
    model had made up."""
    posts = await _kafka_postings(worker_session)
    _answers(monkeypatch, "The one from before still stands [P1].")
    earlier = {
        "question": "Which jobs use Kafka?",
        "answer": "Stream Engineer [P1].",
        "mail_in_context": False,
        "cited": [str(posts["required"].id)],
    }

    body = (await client.post("/chat", json={"message": "hello", "history": [earlier]})).json()

    assert body["unsupported_citations"] == []
    assert body["postings_supplied"] == 1, "nothing was searched, and one was read again"
    assert [s["title"] for s in body["sources"]] == ["Stream Engineer"]


async def test_a_label_past_what_was_carried_over_is_still_invented(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    """One posting was read again, as P1. "[P2]" is nothing the model was given."""
    posts = await _kafka_postings(worker_session)
    _answers(monkeypatch, "That one [P1], and another [P2].")
    earlier = {
        "question": "Which jobs use Kafka?",
        "answer": "Stream Engineer [P1].",
        "mail_in_context": False,
        "cited": [str(posts["required"].id)],
    }

    body = (await client.post("/chat", json={"message": "hello", "history": [earlier]})).json()

    assert body["unsupported_citations"] == ["P2"]


# --------------------------------------------------------------------------
# The counts, each under its own name
# --------------------------------------------------------------------------


async def test_the_numbers_a_reply_carries_are_told_apart(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    await _kafka_postings(worker_session)
    _answers(monkeypatch, "ok")

    body = (await client.post("/chat", json={"message": "Which jobs use Kafka?"})).json()

    assert body["postings_open"] == 3, "every open posting"
    assert body["postings_searched"] == 3, "the ones the search could read"
    assert body["postings_ranked"] == 3, "the ones it found and put in order"
    assert body["postings_supplied"] == len(body["sources"]) == 3, "the ones the model saw"
    assert body["postings_reranked"] == 0, "no second model is configured"
    assert body["gap_postings_counted"] is None, "not a question about gaps"


async def test_a_gap_answer_says_how_many_postings_its_table_was_counted_over(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    await _kafka_postings(worker_session)
    _answers(monkeypatch, "ok")

    body = (await client.post("/chat", json={"message": "What am I lagging on?"})).json()

    assert body["gap_postings_counted"] == 3
    assert body["postings_supplied"] == 0 and body["postings_ranked"] == 0


async def test_an_answer_that_searched_nothing_carries_no_corpus_size(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    """Zeroes, not the size of a corpus nobody read."""
    await _kafka_postings(worker_session)
    _answers(monkeypatch, "ok")

    body = (await client.post("/chat", json={"message": "hello"})).json()

    assert body["postings_open"] == body["postings_searched"] == body["postings_ranked"] == 0


@pytest.mark.parametrize("route", ["/chat", "/chat/stream"])
@pytest.mark.parametrize(
    "question",
    ["Why is my application waiting for review?", "How many applications need my review?"],
)
async def test_selected_application_keeps_its_full_context(
    client: AsyncClient, worker_session, monkeypatch, route, question
) -> None:
    """The review dock must retain parked questions and global queue context."""
    profile, _ = await _owner(worker_session)
    parked_question = "Which portfolio sample should we use?"
    application = Application(
        candidate_id=profile.candidate_id,
        profile_id=profile.id,
        url="https://example.test/jobs/990000099",
        status="needs_review",
        review_json={"unanswered": [{"question": parked_question}]},
    )
    worker_session.add(application)
    await worker_session.commit()
    seen = _answers(monkeypatch, "The selected application's review details are available.")

    response = await client.post(
        route, json={"message": question, "application_id": str(application.id)}
    )

    assert response.status_code == 200
    assert "THE APPLICATION BEING ASKED ABOUT:" in seen["user"]
    assert "questions it is parked on:" in seen["user"]
    assert parked_question in seen["user"]
    assert "APPLICATIONS BY STATUS:" in seen["user"]
    assert "WAITING ON YOU:" in seen["user"]
    body = (
        next(
            frame["reply"]
            for frame in map(json.loads, response.text.splitlines())
            if frame["type"] == "done"
        )
        if route.endswith("stream")
        else response.json()
    )
    assert body["provider"] == "ollama"
    assert body["reply"] == "The selected application's review details are available."


def test_grounded_is_documented_as_where_the_answer_came_from() -> None:
    """Held in the schema, because the field's name promises more than it means."""
    described = ChatReply.model_json_schema()["properties"]["grounded"]["description"]

    assert "not been checked" in described


# --------------------------------------------------------------------------
# How a posting asks for the skill that was named
# --------------------------------------------------------------------------


async def test_each_posting_says_how_it_asks_for_the_skill(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    """All three contain the word. One requires it."""
    await _kafka_postings(worker_session)
    seen = _answers(monkeypatch, "ok")

    await client.post("/chat", json={"message": "Which jobs use Kafka?"})

    context = seen["user"]
    assert 'Kafka here: required — "5+ years running Kafka in production"' in context
    assert "offered as one of several options, so not this one specifically" in context
    assert '"Experience with Kafka or Kinesis"' in context
    assert "named, under no requirements heading, so not shown to be asked for" in context


async def test_a_posting_whose_requirements_were_not_read_says_that(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    profile, company = await _owner(worker_session)
    await _target(worker_session, profile, company, "Kafka Stream Engineer", "", None)
    await worker_session.commit()
    seen = _answers(monkeypatch, "ok")

    await client.post("/chat", json={"message": "Which jobs use Kafka?"})

    assert "Kafka here: this posting's requirements have not been read yet" in seen["user"]


async def test_a_question_naming_no_skill_gets_no_such_line(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    profile, company = await _owner(worker_session)
    await _target(
        worker_session,
        profile,
        company,
        "Stream Engineer",
        "Weekend shifts on the support desk.",
        _requirements(required=["kafka"]),
    )
    await worker_session.commit()
    seen = _answers(monkeypatch, "ok")

    await client.post("/chat", json={"message": "Which jobs have weekend shifts?"})

    assert " here: " not in seen["user"]


# --------------------------------------------------------------------------
# Nothing found, and nothing looked for
# --------------------------------------------------------------------------


async def test_nothing_found_says_how_much_was_read_to_find_it(
    client: AsyncClient, worker_session, monkeypatch
) -> None:
    await _kafka_postings(worker_session)
    seen = _answers(monkeypatch, "ok")

    body = (await client.post("/chat", json={"message": "Which jobs use Zig?"})).json()

    assert "Searched 3 of 3 open postings" in body["reply"]
    assert body["provider"] == "facts"
    assert seen == {}
    assert body["sources"] == [] and body["postings_ranked"] == 0


# --------------------------------------------------------------------------
# Both routes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["unsupported_citations", "postings_supplied", "postings_open"])
async def test_the_stream_closes_with_the_same_account(
    client: AsyncClient, worker_session, monkeypatch, field
) -> None:
    await _kafka_postings(worker_session)
    _answers(monkeypatch, "See [P1] and [P9].")
    question = {"message": "Which jobs use Kafka?"}

    plain = (await client.post("/chat", json=question)).json()
    streamed = await client.post("/chat/stream", json=question)
    [closing] = [
        frame for frame in map(json.loads, streamed.text.splitlines()) if frame["type"] == "done"
    ]

    assert closing["reply"][field] == plain[field]
