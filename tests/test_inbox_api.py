"""Inbound message routes.

packages/inbox/ was fully built and completely unreachable — no router meant a
recruiter reply could move an application and leave no way to see what it said.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.enums import Classification
from packages.core.models import InboundMessage


@pytest.fixture
async def seeded(client: AsyncClient, worker_session: AsyncSession, complete_candidate) -> dict:
    """One application with a routed reply, plus a reply that routed nowhere."""
    created = await client.post(
        "/applications", json={**complete_candidate, "url": "https://boards.greenhouse.io/x/jobs/1"}
    )
    application_id = created.json()["id"]
    candidate_id = complete_candidate["candidate_id"]

    worker_session.add_all(
        [
            InboundMessage(
                candidate_id=uuid.UUID(candidate_id),
                application_id=uuid.UUID(application_id),
                from_addr="recruiter@acme.example",
                subject="Interview invitation",
                body="Are you free Thursday?",
                classification=Classification.INTERVIEW.value,
            ),
            InboundMessage(
                candidate_id=uuid.UUID(candidate_id),
                application_id=None,
                from_addr="stranger@example.com",
                subject="Unrelated",
                body="No alias in the headers.",
                classification=Classification.NOISE.value,
            ),
        ]
    )
    await worker_session.commit()
    return {"application_id": application_id, "candidate_id": candidate_id}


async def test_thread_for_an_application(client: AsyncClient, seeded) -> None:
    listed = await client.get(f"/inbox/for-application/{seeded['application_id']}")
    assert listed.status_code == 200, listed.text
    body = listed.json()

    assert len(body) == 1
    # The recruiter's own words, kept verbatim — the classification is a guess
    # and the owner needs the original to check it against.
    assert body[0]["subject"] == "Interview invitation"
    assert body[0]["body"] == "Are you free Thursday?"
    assert body[0]["classification"] == "interview"


async def test_unrouted_messages_are_surfaced(client: AsyncClient, seeded) -> None:
    """A reply that matched no application is a signal, not something to hide."""
    listed = await client.get("/inbox/unrouted")
    assert listed.status_code == 200

    subjects = [m["subject"] for m in listed.json()]
    assert "Unrelated" in subjects
    assert "Interview invitation" not in subjects


async def test_filters_combine(client: AsyncClient, seeded) -> None:
    listed = await client.get(
        "/inbox", params={"candidate_id": seeded["candidate_id"], "classification": "interview"}
    )
    assert listed.status_code == 200
    assert [m["subject"] for m in listed.json()] == ["Interview invitation"]


async def test_thread_for_unknown_application_is_404(client: AsyncClient) -> None:
    missing = await client.get("/inbox/for-application/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


async def test_limit_is_bounded(client: AsyncClient, seeded) -> None:
    """An unbounded mailbox query is a footgun waiting for a busy inbox."""
    too_many = await client.get("/inbox", params={"limit": 10_000})
    assert too_many.status_code == 400
    assert too_many.json()["error"]["code"] == "invalid_request"
