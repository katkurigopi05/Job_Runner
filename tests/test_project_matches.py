"""Claude Code can ask which jobs match the owner's projects, through the API.

`project_matches` is the assistant's graph answer without a model: the API
route runs `graph_rag.retrieve_graph` and returns the best five with their
source-backed evidence, and the MCP tool reaches it like every other tool.
"""

from __future__ import annotations

from sqlalchemy import select

from packages.core.models import Candidate, Posting, User
from packages.matching import skill_links
from tests.test_graph_rag import seed
from tests.test_mcp_ids import _asked_for


async def test_the_route_returns_the_graph_answer_without_a_model(
    client, committing_sessionmaker
) -> None:
    async with committing_sessionmaker() as session:
        matched = await seed(session)

    response = await client.get("/projects/job-matches")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"] == "top_matches"
    assert [job["posting_id"] for job in body["jobs"]] == [str(matched.id)]
    assert "source-project" in body["jobs"][0]["evidence"]
    assert "Python" in body["jobs"][0]["evidence"]


async def test_with_saved_links_it_reads_every_open_posting(
    client, committing_sessionmaker
) -> None:
    async with committing_sessionmaker() as session:
        await seed(session)
        ids = list((await session.scalars(select(Posting.id))).all())
        await skill_links.link_postings(session, ids)
        await session.commit()

    body = (await client.get("/projects/job-matches")).json()

    assert body["scope"] == "open_postings"
    assert body["searched"] == 1
    assert "every open, unskipped posting" in body["summary"]


async def test_two_candidates_are_never_mixed(client, committing_sessionmaker) -> None:
    async with committing_sessionmaker() as session:
        await seed(session)
        user = User(email="second-owner@example.test")
        session.add(user)
        await session.flush()
        session.add(Candidate(user_id=user.id, name="Other", email="other@example.test"))
        await session.commit()

    body = (await client.get("/projects/job-matches")).json()

    assert body["jobs"] == []
    assert "multiple candidates" in body["summary"]


async def test_the_tool_goes_through_the_api(monkeypatch) -> None:
    assert await _asked_for("project_matches", {}, monkeypatch) == ["/projects/job-matches"]
