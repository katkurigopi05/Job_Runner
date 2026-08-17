"""The match feed — §9 Phase 5's read side."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Match, Posting


@pytest.fixture
async def scored(client: AsyncClient, worker_session: AsyncSession, complete_candidate) -> dict:
    profile_id = uuid.UUID(complete_candidate["profile_id"])

    strong = Posting(url="https://boards.greenhouse.io/acme/jobs/1", title="Staff Backend Engineer")
    weak = Posting(url="https://boards.greenhouse.io/acme/jobs/2", title="Sales Development Rep")
    worker_session.add_all([strong, weak])
    await worker_session.flush()

    worker_session.add_all(
        [
            Match(
                profile_id=profile_id,
                posting_id=strong.id,
                score=0.88,
                reasons_json={"title_similarity": 0.91, "body_similarity": 0.85, "excluded_by": []},
            ),
            Match(
                profile_id=profile_id,
                posting_id=weak.id,
                score=0.21,
                reasons_json={
                    "title_similarity": 0.2,
                    "body_similarity": 0.22,
                    "excluded_by": ["seniority"],
                },
            ),
        ]
    )
    await worker_session.commit()
    return {"profile_id": str(profile_id), "strong": strong.url, "weak": weak.url}


async def test_best_first(client: AsyncClient, scored) -> None:
    feed = await client.get("/matches", params={"profile_id": scored["profile_id"]})
    assert feed.status_code == 200, feed.text

    rows = feed.json()
    assert [r["url"] for r in rows] == [scored["strong"], scored["weak"]]


async def test_the_reasoning_travels_with_the_score(client: AsyncClient, scored) -> None:
    """A ranking without its reasoning is one the owner has to take on trust."""
    rows = (await client.get("/matches", params={"profile_id": scored["profile_id"]})).json()

    top = rows[0]
    assert top["title_similarity"] == pytest.approx(0.91)
    assert top["body_similarity"] == pytest.approx(0.85)
    # And the hard filter that ruled the weak one out is named, not hidden.
    assert rows[1]["excluded_by"] == ["seniority"]


async def test_min_score_filters(client: AsyncClient, scored) -> None:
    rows = (
        await client.get("/matches", params={"profile_id": scored["profile_id"], "min_score": 0.5})
    ).json()
    assert [r["url"] for r in rows] == [scored["strong"]]


async def test_applied_postings_can_be_hidden(
    client: AsyncClient, scored, complete_candidate
) -> None:
    """Once a search is under way the question is what is left, not what is done."""
    created = await client.post(
        "/applications", json={**complete_candidate, "url": scored["strong"]}
    )
    assert created.status_code == 201, created.text

    rows = (
        await client.get(
            "/matches",
            params={"profile_id": scored["profile_id"], "include_applied": "false"},
        )
    ).json()
    assert [r["url"] for r in rows] == [scored["weak"]]


async def test_unknown_profile_is_404(client: AsyncClient) -> None:
    missing = await client.get(
        "/matches", params={"profile_id": "00000000-0000-0000-0000-000000000000"}
    )
    assert missing.status_code == 404
