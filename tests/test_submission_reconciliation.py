"""Settling a submission nobody can vouch for, after asking the employer.

The ledger backfill fenced every interrupted legacy run as `uncertain`. On the
owner's database that was five, four of them on `failed` applications, and
nothing showed those four anywhere. They are not harmless: another
application's ledger row blocks a job in any state, so each one stopped the
job it names from ever being applied to again, for a reason nobody could see.
Releasing one would not help either. `released` lets its own application try
once more, and a failed application never runs again.

`GET /submissions/unsettled` lists them, and the owner answers each one after
checking with the employer:

- **It was sent.** `POST /applications/{id}/submitted`, which already existed.
- **It was not sent.** `POST /applications/{id}/not-submitted` removes the
  fence, as the worker does itself when a refusal comes before the click, and
  keeps the whole row as evidence in a `note` event. The job can be applied
  to again.

Neither is an MCP tool: the answer comes from somebody who looked.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select

from packages.core import submissions
from packages.core.enums import ApplicationStatus, EventType, FailureReason
from packages.core.models import (
    Application,
    ApplicationEvent,
    AppliedPosting,
    Candidate,
    Profile,
    User,
)

JOB = "https://boards.greenhouse.io/acme/jobs/777"


async def _person(session) -> tuple[uuid.UUID, uuid.UUID]:
    user = User(email=f"{uuid.uuid4().hex}@example.test")
    session.add(user)
    await session.flush()
    person = Candidate(user_id=user.id, name="Fixture", email=user.email)
    session.add(person)
    await session.flush()
    profile = Profile(candidate_id=person.id, label="fixture")
    session.add(profile)
    await session.flush()
    return person.id, profile.id


async def _fenced(
    session,
    *,
    status: str,
    state: str = "uncertain",
    url: str = JOB,
    person: tuple[uuid.UUID, uuid.UUID] | None = None,
) -> Application:
    candidate_id, profile_id = person or await _person(session)
    app = Application(candidate_id=candidate_id, profile_id=profile_id, url=url, status=status)
    if status == ApplicationStatus.FAILED.value:
        app.failure_reason = FailureReason.MANUAL_COMPLETION_REQUIRED.value
    if status == ApplicationStatus.NEEDS_REVIEW.value:
        app.review_json = {"submission_uncertain": True, "attempted_at": "2026-09-02T00:00:00"}
    session.add(app)
    await session.flush()
    fence = await submissions.record(session, app, state=state, source="legacy")
    fence.attempted_at = datetime.now(UTC) - timedelta(days=40)
    await session.commit()
    return app


async def test_an_uncertain_fence_on_a_failed_application_is_listed(
    client: AsyncClient, worker_session
) -> None:
    app = await _fenced(worker_session, status=ApplicationStatus.FAILED.value)

    rows = (await client.get("/submissions/unsettled")).json()

    assert [row["application_id"] for row in rows] == [str(app.id)]
    (row,) = rows
    assert row["state"] == "uncertain"
    assert row["source"] == "legacy"
    assert row["application_status"] == "failed"
    assert row["failure_reason"] == "manual_completion_required"
    assert row["url"] == JOB
    assert row["attempted_at"]


async def test_a_settled_or_running_attempt_is_not_listed(
    client: AsyncClient, worker_session
) -> None:
    """Submitted is settled. A running application's `attempting` row is a
    click in flight, which the worker is about to settle itself."""
    await _fenced(worker_session, status=ApplicationStatus.SUBMITTED.value, state="submitted")
    await _fenced(
        worker_session,
        status=ApplicationStatus.RUNNING.value,
        state="attempting",
        url="https://boards.greenhouse.io/acme/jobs/778",
    )

    assert (await client.get("/submissions/unsettled")).json() == []


async def test_not_sent_removes_the_fence_and_keeps_it_as_evidence(
    client: AsyncClient, worker_session
) -> None:
    app_id = (await _fenced(worker_session, status=ApplicationStatus.FAILED.value)).id

    reply = await client.post(
        f"/applications/{app_id}/not-submitted", json={"note": "checked the portal"}
    )

    assert reply.status_code == 200, reply.text
    assert reply.json()["status"] == "failed"
    assert await worker_session.get(AppliedPosting, app_id, populate_existing=True) is None
    notes = (
        await worker_session.scalars(
            select(ApplicationEvent).where(
                ApplicationEvent.application_id == app_id,
                ApplicationEvent.type == EventType.NOTE.value,
            )
        )
    ).all()
    (note,) = notes
    assert note.payload_json["kind"] == "submission_not_sent"
    assert note.payload_json["by"] == "owner"
    assert note.payload_json["note"] == "checked the portal"
    assert note.payload_json["fence"]["state"] == "uncertain"
    assert note.payload_json["fence"]["source"] == "legacy"
    assert note.payload_json["fence"]["url"] == JOB
    assert (await client.get("/submissions/unsettled")).json() == []


async def test_once_settled_as_not_sent_the_job_can_be_applied_to_again(
    client: AsyncClient, worker_session
) -> None:
    person = await _person(worker_session)
    old = await _fenced(worker_session, status=ApplicationStatus.FAILED.value, person=person)
    new = Application(
        candidate_id=person[0],
        profile_id=person[1],
        url=f"{JOB}?gh_src=feed",
        status=ApplicationStatus.NEEDS_REVIEW.value,
    )
    worker_session.add(new)
    await worker_session.commit()
    assert await submissions.blocking(worker_session, new) is not None

    await client.post(f"/applications/{old.id}/not-submitted", json={})

    assert await submissions.blocking(worker_session, new) is None


async def test_a_parked_uncertain_application_can_then_be_approved_plainly(
    client: AsyncClient, worker_session
) -> None:
    app_id = (await _fenced(worker_session, status=ApplicationStatus.NEEDS_REVIEW.value)).id

    reply = await client.post(f"/applications/{app_id}/not-submitted", json={})

    assert reply.status_code == 200, reply.text
    refreshed = await worker_session.get(Application, app_id, populate_existing=True)
    assert refreshed is not None
    assert "submission_uncertain" not in (refreshed.review_json or {})
    assert refreshed.status == "needs_review"


async def test_a_running_application_is_refused(client: AsyncClient, worker_session) -> None:
    app_id = (
        await _fenced(worker_session, status=ApplicationStatus.RUNNING.value, state="attempting")
    ).id

    reply = await client.post(f"/applications/{app_id}/not-submitted", json={})

    assert reply.status_code == 409
    assert reply.json()["error"]["code"] == "invalid_state"
    assert await worker_session.get(AppliedPosting, app_id, populate_existing=True) is not None


async def test_a_submitted_record_is_refused(client: AsyncClient, worker_session) -> None:
    app_id = (
        await _fenced(worker_session, status=ApplicationStatus.SUBMITTED.value, state="submitted")
    ).id

    reply = await client.post(f"/applications/{app_id}/not-submitted", json={})

    assert reply.status_code == 409
    fence = await worker_session.get(AppliedPosting, app_id, populate_existing=True)
    assert fence is not None
    assert fence.state == "submitted"


async def test_nothing_recorded_is_refused(client: AsyncClient, worker_session) -> None:
    candidate_id, profile_id = await _person(worker_session)
    app = Application(
        candidate_id=candidate_id,
        profile_id=profile_id,
        url=JOB,
        status=ApplicationStatus.FAILED.value,
    )
    worker_session.add(app)
    await worker_session.commit()

    reply = await client.post(f"/applications/{app.id}/not-submitted", json={})

    assert reply.status_code == 409


async def test_an_unknown_application_is_not_found(client: AsyncClient) -> None:
    reply = await client.post(f"/applications/{uuid.uuid4()}/not-submitted", json={})

    assert reply.status_code == 404
    assert reply.json()["error"]["code"] == "not_found"


def test_the_mcp_server_has_no_tool_for_it() -> None:
    """The answer is somebody's who checked with the employer, not a model's."""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "apps/mcp/server.py").read_text(
        encoding="utf-8"
    )
    assert "not-submitted" not in source
    assert "/submissions/unsettled" not in source


def test_the_finish_page_offers_both_answers() -> None:
    """Three files have to agree for a row to be answerable at all: the page
    asks for the list, the component offers both answers, and the actions
    reach the two routes."""
    from pathlib import Path

    web = Path(__file__).resolve().parent.parent / "apps/web/src"
    page = (web / "app/finish/page.tsx").read_text(encoding="utf-8")
    component = (web / "components/unsettled-submissions.tsx").read_text(encoding="utf-8")
    actions = (web / "app/finish/actions.ts").read_text(encoding="utf-8")
    client = (web / "lib/api.ts").read_text(encoding="utf-8")

    assert "api.unsettledSubmissions()" in page
    assert "<UnsettledSubmissions" in page
    assert "recordSubmitted(" in component
    assert "recordNotSubmitted(" in component
    assert "api.markNotSubmitted(" in actions
    assert "/not-submitted`" in client
    assert '"/submissions/unsettled"' in client
