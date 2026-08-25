"""API tests — Gate 0.

Covers the two gate assertions that need HTTP: duplicate (candidate_id, url)
returns 409, and the shared error envelope holds everywhere.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

APPLY_URL = "https://boards.greenhouse.io/acme/jobs/12345"


async def test_health(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "api": "ok", "database": "ok"}


async def test_an_unmatched_path_uses_the_shared_envelope(client: AsyncClient) -> None:
    """§10 promises one error shape. A 404 had two.

    `ApiError(NOT_FOUND, ...)` produced the envelope; a 404 for a path with no
    route fell through to FastAPI's `{"detail": "Not Found"}`. A client parsing
    errors got a different shape for the same status depending on how the 404
    arose.
    """
    r = await client.get("/no-such-route")

    assert r.status_code == 404
    assert r.json() == {"error": {"code": "not_found", "message": "Not Found"}}


async def test_a_wrong_method_keeps_its_own_status(client: AsyncClient) -> None:
    """405 is derived from the status, not routed back through STATUS_BY_CODE.

    Mapping the code back to a status would rewrite this into a 400.
    """
    r = await client.delete("/health")

    assert r.status_code == 405
    assert r.json()["error"]["code"] == "invalid_request"


async def test_health_reports_a_database_it_cannot_reach(client: AsyncClient, monkeypatch) -> None:
    """The check has to be able to fail, or it is not a check.

    `/health` returned a hardcoded `{"status": "ok"}`, so Postgres could be
    stopped and it still said ok — and the dashboard indicator built on it would
    have reported healthy while every page that loads data threw.
    """
    from packages.core import db as core_db

    def broken():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(core_db, "get_sessionmaker", broken)

    r = await client.get("/health")

    # Still 200: the process is answering, and a 503 would make the indicator
    # unreachable exactly when it has something to report.
    assert r.status_code == 200
    assert r.json() == {"status": "degraded", "api": "ok", "database": "down"}


async def test_create_candidate_and_profile(client: AsyncClient) -> None:
    r = await client.post("/candidates", json={"name": "Owner", "email": "o@example.com"})
    assert r.status_code == 201
    assert r.json()["email_mode"] == "self"

    r2 = await client.post(
        "/profiles",
        json={
            "candidate_id": r.json()["id"],
            "label": "backend",
            "phone": "+1-555-0100",
            "location": "Austin, TX",
            "work_auth": "US citizen",
            "needs_sponsorship": False,
        },
    )
    assert r2.status_code == 201
    assert r2.json()["auto_submit"] is False  # shipped default, CLAUDE.md §2.3


async def test_create_application(client: AsyncClient, complete_candidate) -> None:
    r = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})

    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "queued"
    assert body["failure_reason"] is None


async def test_duplicate_application_returns_409(client: AsyncClient, complete_candidate) -> None:
    """Gate 0: duplicate (candidate_id, url) returns 409."""
    payload = {**complete_candidate, "url": APPLY_URL}
    first = await client.post("/applications", json=payload)
    assert first.status_code == 201

    second = await client.post("/applications", json=payload)

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "duplicate_application"


async def test_same_url_different_candidate_is_allowed(
    client: AsyncClient, complete_candidate
) -> None:
    first = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})
    assert first.status_code == 201

    other = await client.post("/candidates", json={"name": "Other", "email": "other@example.com"})
    other_profile = await client.post(
        "/profiles",
        json={
            "candidate_id": other.json()["id"],
            "label": "backend",
            "phone": "+1-555-0199",
            "location": "Remote",
            "work_auth": "US citizen",
            "needs_sponsorship": False,
        },
    )
    from tests.conftest import _attach_resume

    await _attach_resume(client, other.json()["id"], other_profile.json()["id"])

    second = await client.post(
        "/applications",
        json={
            "candidate_id": other.json()["id"],
            "profile_id": other_profile.json()["id"],
            "url": APPLY_URL,
        },
    )
    assert second.status_code == 201


# --------------------------------------------------------------------------
# incomplete_candidate is rejected at the boundary, not enqueued to fail
# --------------------------------------------------------------------------


async def test_incomplete_profile_is_rejected_with_400(client: AsyncClient) -> None:
    cand = await client.post("/candidates", json={"name": "Owner", "email": "o2@example.com"})
    prof = await client.post(
        "/profiles",
        json={"candidate_id": cand.json()["id"], "label": "bare"},  # no phone/location/auth
    )

    r = await client.post(
        "/applications",
        json={
            "candidate_id": cand.json()["id"],
            "profile_id": prof.json()["id"],
            "url": APPLY_URL,
        },
    )

    assert r.status_code == 400
    error = r.json()["error"]
    assert error["code"] == "invalid_request"
    for field in ("profile.phone", "profile.location", "profile.work_auth"):
        assert field in error["message"]


async def test_rejected_application_is_never_created(client: AsyncClient) -> None:
    """No row, no queued task — nothing to fail later."""
    cand = await client.post("/candidates", json={"name": "Owner", "email": "o3@example.com"})
    prof = await client.post("/profiles", json={"candidate_id": cand.json()["id"], "label": "bare"})

    await client.post(
        "/applications",
        json={
            "candidate_id": cand.json()["id"],
            "profile_id": prof.json()["id"],
            "url": APPLY_URL,
        },
    )

    listing = await client.get("/applications")
    assert listing.json() == []


async def test_needs_sponsorship_false_is_a_real_answer(client: AsyncClient) -> None:
    """False must not be mistaken for unanswered."""
    cand = await client.post("/candidates", json={"name": "Owner", "email": "o4@example.com"})
    prof = await client.post(
        "/profiles",
        json={
            "candidate_id": cand.json()["id"],
            "label": "p",
            "phone": "+1-555-0100",
            "location": "Austin, TX",
            "work_auth": "US citizen",
            "needs_sponsorship": False,
        },
    )

    from tests.conftest import _attach_resume

    await _attach_resume(client, cand.json()["id"], prof.json()["id"])

    r = await client.post(
        "/applications",
        json={
            "candidate_id": cand.json()["id"],
            "profile_id": prof.json()["id"],
            "url": APPLY_URL,
        },
    )
    assert r.status_code == 201


async def test_unanswered_sponsorship_is_rejected(client: AsyncClient) -> None:
    cand = await client.post("/candidates", json={"name": "Owner", "email": "o5@example.com"})
    prof = await client.post(
        "/profiles",
        json={
            "candidate_id": cand.json()["id"],
            "label": "p",
            "phone": "+1-555-0100",
            "location": "Austin, TX",
            "work_auth": "US citizen",
        },
    )

    r = await client.post(
        "/applications",
        json={
            "candidate_id": cand.json()["id"],
            "profile_id": prof.json()["id"],
            "url": APPLY_URL,
        },
    )
    assert r.status_code == 400
    assert "profile.needs_sponsorship" in r.json()["error"]["message"]


# --------------------------------------------------------------------------
# Error envelope
# --------------------------------------------------------------------------


async def test_not_found_envelope(client: AsyncClient) -> None:
    r = await client.get(f"/applications/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


async def test_validation_error_envelope(client: AsyncClient) -> None:
    r = await client.post("/candidates", json={"name": ""})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert "message" in r.json()["error"]


async def test_unknown_profile_is_invalid_request(client: AsyncClient) -> None:
    cand = await client.post("/candidates", json={"name": "Owner", "email": "o6@example.com"})
    r = await client.post(
        "/applications",
        json={
            "candidate_id": cand.json()["id"],
            "profile_id": str(uuid.uuid4()),
            "url": APPLY_URL,
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


async def test_profile_must_belong_to_candidate(client: AsyncClient, complete_candidate) -> None:
    other = await client.post("/candidates", json={"name": "Other", "email": "o7@example.com"})
    r = await client.post(
        "/applications",
        json={
            "candidate_id": other.json()["id"],
            "profile_id": complete_candidate["profile_id"],
            "url": APPLY_URL,
        },
    )
    assert r.status_code == 400
    assert "does not belong" in r.json()["error"]["message"]


# --------------------------------------------------------------------------
# Review gate
# --------------------------------------------------------------------------


async def test_review_on_a_queued_application_is_invalid_state(
    client: AsyncClient, complete_candidate
) -> None:
    created = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})

    r = await client.post(f"/applications/{created.json()['id']}/review", json={"approve": True})

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "invalid_state"


async def test_creation_writes_an_event(client: AsyncClient, complete_candidate) -> None:
    created = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})

    events = await client.get(f"/applications/{created.json()['id']}/events")

    assert events.status_code == 200
    assert [e["type"] for e in events.json()] == ["created"]


# --------------------------------------------------------------------------
# ATS detection
# --------------------------------------------------------------------------


async def test_detect_greenhouse(client: AsyncClient) -> None:
    r = await client.post("/detect", json={"url": "https://boards.greenhouse.io/acme/jobs/4012345"})
    assert r.status_code == 200
    assert r.json() == {
        "url": "https://boards.greenhouse.io/acme/jobs/4012345",
        "ats": "greenhouse",
        "supported": True,
    }


async def test_detect_unknown_site(client: AsyncClient) -> None:
    r = await client.post("/detect", json={"url": "https://acme.com/careers/1"})
    assert r.json()["ats"] is None
    assert r.json()["supported"] is False


async def test_list_supported_ats(client: AsyncClient) -> None:
    r = await client.get("/ats")
    assert r.status_code == 200
    assert "greenhouse" in r.json()


# --------------------------------------------------------------------------
# OTP — needs_otp must have a way out
# --------------------------------------------------------------------------


async def _park_at_needs_otp(client: AsyncClient, complete_candidate, worker_session) -> str:
    from packages.core.enums import ApplicationStatus
    from packages.core.models import Application
    from packages.core.state import transition

    created = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})
    app_id = created.json()["id"]

    application = await worker_session.get(Application, app_id)
    await transition(worker_session, application, ApplicationStatus.RUNNING)
    await transition(worker_session, application, ApplicationStatus.NEEDS_OTP)
    await worker_session.commit()
    return app_id


async def test_otp_resumes_a_parked_application(
    client: AsyncClient, complete_candidate, worker_session
) -> None:
    app_id = await _park_at_needs_otp(client, complete_candidate, worker_session)

    r = await client.post(f"/applications/{app_id}/otp", json={"code": "123456"})

    assert r.status_code == 200
    assert r.json()["status"] == "running"


async def test_otp_code_is_not_written_to_the_event_log(
    client: AsyncClient, complete_candidate, worker_session
) -> None:
    """The audit log is append-only; a verification code does not belong in it."""
    app_id = await _park_at_needs_otp(client, complete_candidate, worker_session)
    await client.post(f"/applications/{app_id}/otp", json={"code": "987654"})

    events = (await client.get(f"/applications/{app_id}/events")).json()
    assert "987654" not in str(events)
    assert any(e.get("payload", {}).get("otp_supplied") for e in events)


async def test_otp_on_a_queued_application_is_invalid_state(
    client: AsyncClient, complete_candidate
) -> None:
    created = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})
    r = await client.post(f"/applications/{created.json()['id']}/otp", json={"code": "1"})

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "invalid_state"
