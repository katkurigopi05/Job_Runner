"""The feed shows one card per requisition, and forgets nothing about the copies."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from packages.core.models import Application, Company, Match, Posting
from packages.matching.canonical import assign

BODY = (
    "We are hiring a backend engineer to build payment infrastructure. You will design "
    "APIs, operate PostgreSQL at scale, and mentor engineers on reliability practices. "
    "Requirements: Python, distributed systems experience, and on-call ownership."
)


@pytest.fixture
async def copies(client: AsyncClient, worker_session, complete_candidate) -> dict:
    profile_id = uuid.UUID(complete_candidate["profile_id"])
    company = Company(name="Grouped Co")
    worker_session.add(company)
    await worker_session.flush()
    board = Posting(
        company_id=company.id,
        external_id="1",
        ats_type="greenhouse",
        url="https://boards.greenhouse.io/grouped/jobs/1",
        title="Backend Engineer",
        location="Remote — US",
        description_raw=BODY,
    )
    careers = Posting(
        company_id=company.id,
        external_id="careers-1",
        ats_type="jsonld",
        url="https://grouped.example/careers/backend",
        title="Backend Engineer",
        location="Remote — US",
        description_raw=BODY,
    )
    alone = Posting(
        company_id=company.id,
        external_id="2",
        ats_type="greenhouse",
        url="https://boards.greenhouse.io/grouped/jobs/2",
        title="Backend Engineer",
        location="Remote — US",
        description_raw=BODY + " A second opening on the same board.",
    )
    worker_session.add_all([board, careers, alone])
    await worker_session.flush()
    await assign(worker_session, [board.id, careers.id, alone.id])
    worker_session.add_all(
        [
            Match(profile_id=profile_id, posting_id=board.id, score=0.9, reasons_json={}),
            Match(profile_id=profile_id, posting_id=careers.id, score=0.8, reasons_json={}),
            Match(profile_id=profile_id, posting_id=alone.id, score=0.7, reasons_json={}),
        ]
    )
    await worker_session.commit()
    return {
        "profile_id": str(profile_id),
        "board": str(board.id),
        "careers": str(careers.id),
        "careers_url": careers.url,
        "alone": str(alone.id),
        "candidate_id": complete_candidate["candidate_id"],
    }


async def _feed(client: AsyncClient, copies: dict, **params) -> list[dict]:
    response = await client.get(
        "/matches", params={"profile_id": copies["profile_id"], "us_only": "false", **params}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_copies_become_one_card_listing_both_sources(client, copies) -> None:
    feed = await _feed(client, copies)

    ids = [row["posting_id"] for row in feed]
    assert ids == [copies["board"], copies["alone"]], "the second opening on one board stays"
    card = feed[0]
    assert {source["posting_id"] for source in card["sources"]} == {
        copies["board"],
        copies["careers"],
    }
    assert feed[1]["sources"] == []


async def test_a_decision_on_a_copy_is_shown_and_not_offered_again(
    client, copies, worker_session
) -> None:
    await client.post(
        f"/matches/{await _match_id(client, copies, copies['careers'])}/decision",
        json={"decision": "skipped"},
    )

    card = (await _feed(client, copies))[0]
    by_id = {source["posting_id"]: source for source in card["sources"]}
    assert by_id[copies["careers"]]["decision"] == "skipped"

    undecided = await _feed(client, copies, undecided_only="true")
    assert [row["posting_id"] for row in undecided] == [copies["alone"]]


async def test_an_application_through_a_copy_hides_the_requisition(
    client, copies, worker_session
) -> None:
    profile = uuid.UUID(copies["profile_id"])
    worker_session.add(
        Application(
            candidate_id=uuid.UUID(copies["candidate_id"]),
            profile_id=profile,
            posting_id=uuid.UUID(copies["careers"]),
            url=copies["careers_url"],
            status="needs_review",
        )
    )
    await worker_session.commit()

    hidden = await _feed(client, copies, include_applied="false")

    assert [row["posting_id"] for row in hidden] == [copies["alone"]]


async def _match_id(client: AsyncClient, copies: dict, posting_id: str) -> str:
    from sqlalchemy import select

    from packages.core import db as core_db

    async with core_db.get_sessionmaker()() as session:
        return str(
            await session.scalar(select(Match.id).where(Match.posting_id == uuid.UUID(posting_id)))
        )
