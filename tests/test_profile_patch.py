"""Regression tests for PATCH /profiles/{id}."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.fixture
async def profile(client: AsyncClient) -> dict:
    cand = await client.post(
        "/candidates", json={"name": "Edit Owner", "email": "edit@example.com"}
    )
    prof = await client.post(
        "/profiles",
        json={
            "candidate_id": cand.json()["id"],
            "label": "default",
            "phone": "+1-555-0100",
            "location": "Austin, TX",
            "work_auth": "US citizen",
            "needs_sponsorship": False,
            "min_match_score": 0.75,
        },
    )
    assert prof.status_code == 201, prof.text
    return prof.json()


async def test_patch_changes_only_what_was_sent(client: AsyncClient, profile) -> None:
    """A form posting one field must not blank the other nine."""
    updated = await client.patch(f"/profiles/{profile['id']}", json={"location": "Remote, US"})
    assert updated.status_code == 200, updated.text
    body = updated.json()

    assert body["location"] == "Remote, US"
    # §2.2 — a partial edit silently clearing this would put a blank
    # work-authorization answer on a real application.
    assert body["work_auth"] == "US citizen"
    assert body["phone"] == "+1-555-0100"
    assert body["min_match_score"] == 0.75


async def test_explicit_null_clears(client: AsyncClient, profile) -> None:
    """Sent-as-null and not-sent are different intentions."""
    updated = await client.patch(f"/profiles/{profile['id']}", json={"salary_expectation": None})
    assert updated.status_code == 200
    assert updated.json()["salary_expectation"] is None


async def test_unknown_profile_is_404(client: AsyncClient) -> None:
    missing = await client.patch(
        "/profiles/00000000-0000-0000-0000-000000000000", json={"label": "x"}
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


async def test_score_bounds_are_enforced(client: AsyncClient, profile) -> None:
    """Out-of-range comes back in the §10 envelope, not FastAPI's own 422."""
    bad = await client.patch(f"/profiles/{profile['id']}", json={"min_match_score": 1.5})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_request"
