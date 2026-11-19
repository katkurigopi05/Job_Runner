"""A project question can name a topic, and Qwen finds the jobs about it.

"Which jobs match my projects related to RAG pipelines?" embeds the topic on
this machine with Qwen3-Embedding-4B, takes the open postings nearest to it,
and the graph keeps those that directly require a technology the owner's
projects declare. A posting about the topic need not use its words. When
Qwen cannot embed the topic the question is refused with the reason; it is
never answered another way.
"""

from __future__ import annotations

import pytest

from apps.api.routers import chat
from packages.matching import chunk_search, graph_questions, skill_links
from packages.matching.graph_questions import TOPIC_NEAREST, graph_intent
from packages.matching.graph_rag import retrieve_graph
from packages.matching.query_vectors import QueryEmbeddingError, QueryVector
from tests.test_graph_rag import posting, seed

OLLAMA_DOWN = "Ollama is not reachable at http://127.0.0.1:11434. Start it with `ollama serve`."


def _topic_search(monkeypatch, nearest: list) -> list[dict]:
    """Stand-ins for Qwen: the embedding call is recorded, the ranking returns `nearest`."""
    calls: list[dict] = []

    async def embed(text, *, keep_alive=None, client=None):  # noqa: ANN001, ANN202
        calls.append({"text": text, "keep_alive": keep_alive})
        return QueryVector(space="qwen", build="test", values=(1.0,), seconds=0.0)

    async def read(self, session, *, among, companies, limit):  # noqa: ANN001, ANN202
        calls.append({"limit": limit, "among": among})
        return [(posting_id, 0, 10) for posting_id in nearest], True

    monkeypatch.setattr(graph_questions, "embed_question", embed)
    monkeypatch.setattr(chunk_search.ShortlistRanking, "read", read)
    return calls


async def test_a_topic_is_embedded_once_and_keeps_the_nearest_postings(
    db_session, monkeypatch
) -> None:
    nearest = [posting().id for _ in range(TOPIC_NEAREST + 5)]
    calls = _topic_search(monkeypatch, nearest)

    question = await graph_intent(
        "Which jobs match my projects related to RAG pipelines?", session=db_session
    )

    assert question is not None and question.intent == "projects"
    assert question.constraint is not None
    assert question.constraint.label == "related to RAG pipelines"
    assert question.constraint.posting_ids == frozenset(nearest[:TOPIC_NEAREST])
    # Unloaded at once: a topic question must not hold the model beside a local chat model.
    assert calls[0] == {"text": "RAG pipelines", "keep_alive": "0"}
    assert calls[1]["among"] is None and calls[1]["limit"] == TOPIC_NEAREST


@pytest.mark.parametrize(
    "question",
    [
        "Which jobs match my projects related to",
        "Which jobs match my projects related to RAG in London",
        "Which jobs match my projects about RAG for data engineer roles",
    ],
)
async def test_no_topic_or_a_second_constraint_is_not_a_topic_question(
    db_session, monkeypatch, question
) -> None:
    """A place or role inside the topic would be searched by meaning and never enforced."""
    _topic_search(monkeypatch, [])
    assert await graph_intent(question, session=db_session) is None


@pytest.mark.parametrize("linked", [False, True])
async def test_the_graph_keeps_only_jobs_about_the_topic(
    worker_session, monkeypatch, linked
) -> None:
    about = await seed(worker_session)
    other = posting(company_id=about.company_id, title="Platform Engineer")
    worker_session.add(other)
    await worker_session.commit()
    if linked:
        from sqlalchemy import select

        from packages.core.models import Posting

        ids = list((await worker_session.scalars(select(Posting.id))).all())
        await skill_links.link_postings(worker_session, ids)
        await worker_session.commit()
    _topic_search(monkeypatch, [about.id])

    question = await graph_intent(
        "Which jobs match my projects about retrieval systems?", session=worker_session
    )
    result = await retrieve_graph(worker_session, question)

    assert [p.posting_id for p in result.found.passages] == [about.id]
    assert "related to retrieval systems" in result.answer_summary


@pytest.mark.parametrize("route", ["/chat", "/chat/stream"])
async def test_a_topic_qwen_cannot_embed_is_refused_with_the_reason(
    client, monkeypatch, route
) -> None:
    async def down(text, *, keep_alive=None, client=None):  # noqa: ANN001, ANN202
        raise QueryEmbeddingError(OLLAMA_DOWN)

    monkeypatch.setattr(graph_questions, "embed_question", down)
    asked: list[str] = []
    monkeypatch.setattr(chat, "provider_for", lambda name: asked.append(name))

    answered = await client.post(
        route,
        json={"message": "Which jobs match my projects related to RAG?", "provider": "ollama"},
    )

    assert answered.status_code == 500
    message = answered.json()["error"]["message"]
    assert "ollama serve" in message and "Nothing was searched" in message
    assert asked == []


async def test_a_topic_answer_lists_the_most_related_jobs_first(
    worker_session, monkeypatch
) -> None:
    """Not the most shared skills first: the question asked about a topic."""
    shares_two = await seed(worker_session)
    shares_one = posting(
        company_id=shares_two.company_id,
        title="Retrieval Engineer",
        body="Requirements\nPython is required.",
    )
    worker_session.add(shares_one)
    await worker_session.commit()
    await skill_links.link_postings(worker_session, [shares_one.id, shares_two.id])
    await worker_session.commit()
    _topic_search(monkeypatch, [shares_one.id, shares_two.id])

    question = await graph_intent(
        "Which jobs match my projects related to retrieval systems?", session=worker_session
    )
    result = await retrieve_graph(worker_session, question)

    assert [p.posting_id for p in result.found.passages] == [shares_one.id, shares_two.id]


@pytest.mark.parametrize("filler", ["a" * 3900, " in" * 1300, "  " * 1950, " for x" * 650])
async def test_a_long_or_pathological_topic_returns_at_once(
    db_session, monkeypatch, filler
) -> None:
    """The question is typed text up to 4,000 characters (`ChatRequest`)."""
    import time

    async def never(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("a topic this long must not be embedded")

    monkeypatch.setattr(graph_questions, "embed_question", never)
    question = f"Which jobs match my projects related to {filler}"[:4000]

    started = time.perf_counter()
    found = await graph_intent(question, session=db_session)

    assert found is None
    assert time.perf_counter() - started < 0.5
