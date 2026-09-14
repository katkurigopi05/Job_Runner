"""Contacts, interview and assessment tasks, reminders, and the calendar export.

Nothing here contacts an employer, and two tests hold that line directly: the
calendar export carries no contact's details, and a reminder goes only to the
owner's configured local backends.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.config import get_settings
from packages.core.models import Application
from packages.core.models_tracking import ApplicationTask
from packages.tracking.reminders import ring_due
from packages.tracking.tasks import DEFAULT_CHECKLISTS, ensure_task_for_outcome


@pytest.fixture
async def app_id(client: AsyncClient, worker_session, complete_candidate) -> str:
    application = Application(
        candidate_id=uuid.UUID(complete_candidate["candidate_id"]),
        profile_id=uuid.UUID(complete_candidate["profile_id"]),
        url="https://boards.greenhouse.io/acme/jobs/77",
        status="submitted",
    )
    worker_session.add(application)
    await worker_session.commit()
    return str(application.id)


async def _task(client: AsyncClient, app_id: str, **body) -> dict:
    payload = {"kind": "interview", "title": "Onsite", **body}
    response = await client.post(f"/applications/{app_id}/tasks", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------


async def test_an_interview_task_starts_with_the_prep_checklist(client, app_id) -> None:
    task = await _task(client, app_id)

    assert [item["text"] for item in task["checklist"]] == list(DEFAULT_CHECKLISTS["interview"])
    assert all(item["done"] is False for item in task["checklist"])
    assert task["source"] == "owner"


async def test_a_reminder_after_the_deadline_is_refused(client, app_id) -> None:
    response = await client.post(
        f"/applications/{app_id}/tasks",
        json={
            "kind": "assessment",
            "title": "Take-home",
            "due_at": "2026-09-20T15:00:00-07:00",
            "reminder_at": "2026-09-21T09:00:00-07:00",
        },
    )

    assert response.status_code == 400


async def test_a_time_without_a_zone_is_refused(client, app_id) -> None:
    response = await client.post(
        f"/applications/{app_id}/tasks",
        json={"kind": "interview", "title": "Call", "due_at": "2026-09-20T15:00:00"},
    )

    assert response.status_code == 400
    assert "time zone" in response.json()["error"]["message"]


async def test_ticking_the_checklist_and_completing(client, app_id) -> None:
    task = await _task(client, app_id, checklist=["Confirm time", "Prepare questions"])
    items = [{"text": "Confirm time", "done": True}, {"text": "Prepare questions", "done": False}]

    patched = await client.patch(
        f"/tasks/{task['id']}", json={"checklist": items, "completed": True}
    )

    body = patched.json()
    assert body["checklist"] == items
    assert body["completed_at"] is not None


async def test_moving_a_reminder_lets_it_ring_again(
    client, app_id, committing_sessionmaker
) -> None:
    task = await _task(
        client,
        app_id,
        due_at="2026-09-20T15:00:00+00:00",
        reminder_at="2026-09-20T14:00:00+00:00",
    )
    async with committing_sessionmaker() as session:
        row = await session.get(ApplicationTask, uuid.UUID(task["id"]))
        row.reminded_at = datetime.now(UTC)
        await session.commit()

    moved = await client.patch(
        f"/tasks/{task['id']}", json={"reminder_at": "2026-09-20T13:00:00+00:00"}
    )

    assert moved.json()["reminded_at"] is None


async def test_upcoming_is_soonest_first_flags_overdue_and_skips_done(client, app_id) -> None:
    now = datetime.now(UTC)
    late = await _task(client, app_id, title="Late", due_at=(now - timedelta(days=1)).isoformat())
    soon = await _task(client, app_id, title="Soon", due_at=(now + timedelta(days=2)).isoformat())
    done = await _task(client, app_id, title="Done", due_at=(now + timedelta(days=1)).isoformat())
    await client.patch(f"/tasks/{done['id']}", json={"completed": True})

    upcoming = (await client.get("/tasks", params={"due_within_days": 14})).json()

    assert [task["title"] for task in upcoming] == ["Late", "Soon"]
    assert upcoming[0]["overdue"] is True and upcoming[1]["overdue"] is False
    assert upcoming[0]["application_url"].endswith("/jobs/77")
    assert {late["id"], soon["id"]} == {task["id"] for task in upcoming}


# --------------------------------------------------------------------------
# Contacts
# --------------------------------------------------------------------------


async def test_a_contact_is_created_linked_and_survives_unlinking(client, app_id) -> None:
    linked = await client.post(
        f"/applications/{app_id}/contacts",
        json={
            "relationship": "recruiter",
            "contact": {"name": "Ada Recruiter", "email": "ada@acme.example", "role": "Talent"},
        },
    )
    assert linked.status_code == 200, linked.text
    contact_id = linked.json()["contact"]["id"]

    tracking = (await client.get(f"/applications/{app_id}/tracking")).json()
    assert [c["contact"]["name"] for c in tracking["contacts"]] == ["Ada Recruiter"]

    assert (await client.delete(f"/applications/{app_id}/contacts/{contact_id}")).status_code == 204
    assert (await client.get(f"/applications/{app_id}/tracking")).json()["contacts"] == []
    assert [c["id"] for c in (await client.get("/contacts")).json()] == [contact_id]


async def test_a_script_link_is_not_stored_as_a_profile_url(client, app_id) -> None:
    response = await client.post(
        f"/applications/{app_id}/contacts",
        json={"contact": {"name": "X", "profile_url": "javascript:alert(1)"}},
    )

    assert response.status_code == 400


# --------------------------------------------------------------------------
# Calendar export
# --------------------------------------------------------------------------


async def test_the_calendar_export_is_valid_escaped_and_carries_no_contact(client, app_id) -> None:
    await client.post(
        f"/applications/{app_id}/contacts",
        json={"contact": {"name": "Ada", "email": "ada@acme.example"}},
    )
    task = await _task(
        client,
        app_id,
        title="Onsite, final round; bring ID",
        due_at="2026-09-20T15:00:00-07:00",
        reminder_at="2026-09-20T14:00:00-07:00",
    )

    response = await client.get(f"/applications/{app_id}/tasks.ics")

    assert response.headers["content-type"].startswith("text/calendar")
    body = response.text
    assert body.startswith("BEGIN:VCALENDAR\r\n") and body.endswith("END:VCALENDAR\r\n")
    assert f"UID:{task['id']}@jobrunner.local" in body
    assert "DTSTART:20260920T220000Z" in body
    assert "SUMMARY:Onsite\\, final round\\; bring ID" in body
    assert "TRIGGER:-PT60M" in body
    assert "ada@acme.example" not in body


# --------------------------------------------------------------------------
# Automatic tasks and reminders
# --------------------------------------------------------------------------


async def test_a_routed_interview_creates_one_task_and_a_rejection_none(
    db_session, application
) -> None:
    first = await ensure_task_for_outcome(db_session, application, "interview")
    again = await ensure_task_for_outcome(db_session, application, "interview")
    rejected = await ensure_task_for_outcome(db_session, application, "rejected")

    assert first is not None and first.source == "inbox" and first.due_at is None
    assert again is None, "a thread of replies does not stack tasks"
    assert rejected is None


async def test_due_reminders_ring_once_and_never_for_completed_tasks(
    app_id, committing_sessionmaker, monkeypatch
) -> None:
    monkeypatch.setenv("NOTIFY_BACKENDS", "log")
    get_settings.cache_clear()
    past = datetime.now(UTC) - timedelta(minutes=5)
    async with committing_sessionmaker() as session:
        session.add_all(
            [
                ApplicationTask(
                    application_id=uuid.UUID(app_id),
                    kind="interview",
                    title="Due",
                    reminder_at=past,
                    checklist_json=[],
                ),
                ApplicationTask(
                    application_id=uuid.UUID(app_id),
                    kind="interview",
                    title="Done already",
                    reminder_at=past,
                    completed_at=past,
                    checklist_json=[],
                ),
            ]
        )
        await session.commit()

    try:
        async with committing_sessionmaker() as session:
            assert await ring_due(session) == 1
        async with committing_sessionmaker() as session:
            assert await ring_due(session) == 0
            rung = await session.scalar(
                select(ApplicationTask).where(ApplicationTask.title == "Due")
            )
        assert rung.reminded_at is not None
    finally:
        get_settings.cache_clear()


async def test_follow_up_reporting_notes_an_open_follow_up_task(
    client, app_id, worker_session
) -> None:
    application = await worker_session.get(Application, uuid.UUID(app_id))
    application.created_at = datetime.now(UTC) - timedelta(days=30)
    await worker_session.commit()
    await _task(client, app_id, kind="follow_up", title="Write a follow-up")

    cadence = (await client.get("/analytics/cadence")).json()

    silent = {item["application_id"]: item for item in cadence["silent"]}
    assert silent[app_id]["has_follow_up_task"] is True
