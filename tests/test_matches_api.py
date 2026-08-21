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


# --------------------------------------------------------------------------
# Search filters — the owner's input, not a reading of their profile
# --------------------------------------------------------------------------


@pytest.fixture
async def varied(client: AsyncClient, worker_session: AsyncSession, complete_candidate) -> str:
    """Postings that differ on every axis a filter can cut."""
    profile_id = uuid.UUID(complete_candidate["profile_id"])

    rows = [
        ("Senior Backend Engineer", "Remote — US", "Python and PostgreSQL.", 0.9),
        ("Junior Backend Engineer", "Austin, TX", "Python, on-site role.", 0.8),
        ("Principal Data Engineer", "Remote — EU", "Spark and Kubernetes.", 0.7),
    ]
    for index, (title, location, body, score) in enumerate(rows):
        posting = Posting(
            url=f"https://boards.greenhouse.io/acme/jobs/{index}",
            title=title,
            location=location,
            description_raw=body,
        )
        worker_session.add(posting)
        await worker_session.flush()
        worker_session.add(
            Match(profile_id=profile_id, posting_id=posting.id, score=score, reasons_json={})
        )
    await worker_session.commit()
    return str(profile_id)


async def _titles(client: AsyncClient, profile_id: str, **params) -> list[str]:
    response = await client.get("/matches", params={"profile_id": profile_id, **params})
    assert response.status_code == 200, response.text
    return [row["title"] for row in response.json()]


async def test_remote_filter(client: AsyncClient, varied) -> None:
    assert await _titles(client, varied, remote="true") == [
        "Senior Backend Engineer",
        "Principal Data Engineer",
    ]
    assert await _titles(client, varied, remote="false") == ["Junior Backend Engineer"]


async def test_seniority_range(client: AsyncClient, varied) -> None:
    assert await _titles(client, varied, min_seniority="senior") == [
        "Senior Backend Engineer",
        "Principal Data Engineer",
    ]
    assert await _titles(client, varied, max_seniority="junior") == ["Junior Backend Engineer"]


async def test_keywords_and_location(client: AsyncClient, varied) -> None:
    assert await _titles(client, varied, keywords="kubernetes") == ["Principal Data Engineer"]
    assert await _titles(client, varied, locations="Austin") == ["Junior Backend Engineer"]


async def test_filters_combine(client: AsyncClient, varied) -> None:
    assert await _titles(client, varied, remote="true", keywords="python") == [
        "Senior Backend Engineer"
    ]


async def test_an_unknown_seniority_is_rejected_not_ignored(client: AsyncClient, varied) -> None:
    """Silently ignoring it would return everything and look like a match."""
    response = await client.get(
        "/matches", params={"profile_id": varied, "min_seniority": "wizard"}
    )
    assert response.status_code == 400
    assert "wizard" in response.json()["error"]["message"]


async def test_the_limit_applies_after_filtering(client: AsyncClient, varied) -> None:
    """A narrow search should still fill a page, not return whatever survived
    an unfiltered slice."""
    titles = await _titles(client, varied, remote="true", limit=2)
    assert len(titles) == 2


async def test_the_summary_counts_rather_than_measuring_a_page(
    client: AsyncClient, complete_candidate: dict[str, str], worker_session
) -> None:
    """`GET /matches` is a page — it caps at 200 and defaults to 50.

    The dashboard read that page length as a total and reported "50 matches"
    against a database holding 1,853. A count has to come from a count, so
    this route exists and the feed route is not asked to be one.
    """
    import uuid as _uuid

    from packages.core.models import Match, Posting

    profile_id = _uuid.UUID(complete_candidate["profile_id"])
    for index in range(60):
        posting = Posting(url=f"https://example.com/summary/{index}", title=f"Role {index}")
        worker_session.add(posting)
        await worker_session.flush()
        worker_session.add(
            Match(
                profile_id=profile_id,
                posting_id=posting.id,
                score=0.5,
                decision="interested" if index < 3 else None,
            )
        )
    await worker_session.commit()

    body = (await client.get("/matches/summary")).json()

    assert body["total"] == 60
    assert body["interested"] == 3
    assert body["undecided"] == 57
    # The feed still returns a page, which is the distinction being drawn.
    assert len((await client.get("/matches?limit=50")).json()) == 50


async def test_a_decision_is_recorded_by_a_plain_post(
    client: AsyncClient, complete_candidate: dict[str, str], worker_session
) -> None:
    """The route the swipe deck reaches, via a Server Action.

    Calling it from the browser instead sent a CORS preflight the API answers
    with 405, so every swipe failed and the page reported the API unreachable.
    The API is deliberately loopback-only and unauthenticated — widening it to
    a browser origin would open exactly what that rule protects — so the
    request belongs on the Next server.
    """
    import uuid as _uuid

    from packages.core.models import Match, Posting

    posting = Posting(url="https://example.com/decide/1", title="Engineer")
    worker_session.add(posting)
    await worker_session.flush()
    match = Match(
        profile_id=_uuid.UUID(complete_candidate["profile_id"]),
        posting_id=posting.id,
        score=0.4,
    )
    worker_session.add(match)
    await worker_session.commit()

    response = await client.post(f"/matches/{match.id}/decision", json={"decision": "interested"})

    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "interested"
    assert (await client.get("/matches/summary")).json()["interested"] == 1


async def test_an_unknown_decision_is_refused(
    client: AsyncClient, complete_candidate: dict[str, str], worker_session
) -> None:
    import uuid as _uuid

    from packages.core.models import Match, Posting

    posting = Posting(url="https://example.com/decide/2", title="Engineer")
    worker_session.add(posting)
    await worker_session.flush()
    match = Match(
        profile_id=_uuid.UUID(complete_candidate["profile_id"]), posting_id=posting.id, score=0.4
    )
    worker_session.add(match)
    await worker_session.commit()

    response = await client.post(f"/matches/{match.id}/decision", json={"decision": "maybe"})

    assert response.status_code == 400
    assert "interested" in response.json()["error"]["message"]
