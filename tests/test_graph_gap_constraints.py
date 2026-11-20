"""The skill-gaps question takes the same constraints as the projects question.

"Which skill gaps recur across jobs in my area for data engineer roles?" counts
the gaps over the IT jobs the constraint keeps, not over every IT job in the
search area. A place, an employer, a role from the role table and a topic
(found with Qwen) each narrow it, in the saved-links path and in the top-100
fallback alike. A suffix that is not one of those is not a gaps question.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from packages.core.models import Posting
from packages.matching import chunk_search, graph_questions, graph_rag
from packages.matching.query_vectors import QueryVector
from tests.test_graph_constraints import fixture

GAPS = "Which skill gaps recur across jobs in my area"


@pytest.mark.parametrize(
    "question,label",
    [
        (f"{GAPS} for data engineer roles?", "for data engineer roles"),
        (f"{GAPS} in California?", "in California"),
        (f"{GAPS} in London?", "in London"),
        ("Which skill gaps recur across my top matches in the Bay Area?", "in the Bay Area"),
        (
            "Which skills do my top matches ask for that I am missing for backend engineer roles?",
            "for backend engineer roles",
        ),
    ],
)
async def test_a_gaps_question_takes_a_place_or_role(question, label) -> None:
    query = await graph_rag.graph_intent(question)

    assert query is not None and query.intent == "gaps"
    assert query.constraint is not None and query.constraint.label == label


@pytest.mark.parametrize(
    "suffix", ["in Narnia", "for wizard roles", "at Unknown Employer", "in London at Acme"]
)
async def test_a_gaps_suffix_it_cannot_read_is_not_a_gaps_question(worker_session, suffix) -> None:
    assert await graph_rag.graph_intent(f"{GAPS} {suffix}?", session=worker_session) is None


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize(
    "suffix,indices",
    [
        ("in California", [0, 1, 2]),
        ("in the Bay Area", [0, 1]),
        ("at Acme", [1, 2, 4]),
        ("for backend engineer roles", [0, 2, 4]),
        ("in Paris", [3]),
    ],
)
async def test_each_constraint_narrows_the_gaps_in_both_paths(
    worker_session, linked, suffix, indices
) -> None:
    await fixture(worker_session, linked)
    query = await graph_rag.graph_intent(f"{GAPS} {suffix}?", session=worker_session)
    assert query is not None and query.constraint is not None

    result = await graph_rag.retrieve_graph(worker_session, query)

    assert result.found.searched == len(indices)
    assert result.scope == ("open_postings" if linked else "top_matches")
    label = query.constraint.label
    answer = result.answer([e.label for e in result.evidence])
    assert label in result.summary and label in answer and label in result.answer_note
    if len(indices) > 1:
        # Every fixture job requires PostgreSQL, which the résumé does not list.
        assert f"directly required by {len(indices)} of {len(indices)}" in answer
    if suffix in {"at Acme", "for backend engineer roles"}:
        assert "within your search area" in result.answer_note
    if suffix == "in Paris":
        assert "standing search area waived" in result.summary
        assert "your search area" not in result.answer_note


async def test_unconstrained_gaps_still_read_every_it_job_in_the_area(worker_session) -> None:
    await fixture(worker_session, True)

    result = await graph_rag.retrieve_graph(worker_session, await graph_rag.graph_intent(GAPS))

    # The fixture's five in-area jobs: Paris and London are outside it.
    assert result.found.searched == 4
    assert "across 4 open IT jobs in your area" in result.answer_summary


async def test_a_gaps_topic_keeps_only_jobs_about_it(worker_session, monkeypatch) -> None:
    jobs = await fixture(worker_session, True)
    about = [jobs[1].id, jobs[2].id]

    async def embed(text, *, keep_alive=None, client=None):  # noqa: ANN001, ANN202
        return QueryVector(space="qwen", build="test", values=(1.0,), seconds=0.0)

    async def read(self, session, *, among, companies, limit):  # noqa: ANN001, ANN202
        return [(posting_id, 0, 10) for posting_id in about], True

    monkeypatch.setattr(graph_questions, "embed_question", embed)
    monkeypatch.setattr(chunk_search.ShortlistRanking, "read", read)

    query = await graph_rag.graph_intent(
        f"{GAPS} related to streaming pipelines?", session=worker_session
    )
    assert query is not None and query.intent == "gaps"
    result = await graph_rag.retrieve_graph(worker_session, query)

    assert result.found.searched == 2
    assert "related to streaming pipelines" in result.answer_summary
    assert "directly required by 2 of 2" in result.answer([e.label for e in result.evidence])


async def test_chat_answers_a_constrained_gaps_question_from_the_graph(
    client, worker_session, monkeypatch
) -> None:
    from apps.api.routers import chat
    from packages.llm.provider import StubProvider

    await fixture(worker_session, True)
    assert await worker_session.scalar(select(Posting.id).limit(1)) is not None

    class Selector(StubProvider):
        async def complete(self, system, user, **kwargs):  # noqa: ANN001, ANN003, ANN202
            return '{"ids": ["P1"]}'

    monkeypatch.setattr(chat, "provider_for", lambda selected: (Selector(), "local-model"))

    response = await client.post("/chat", json={"message": f"{GAPS} for backend engineer roles?"})

    assert response.status_code == 200
    result = response.json()
    assert result["retrieval_method"] == "graph"
    assert "for backend engineer roles" in result["reply"]
    assert "directly required by 3 of 3" in result["reply"]
