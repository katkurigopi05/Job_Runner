"""The tracker over MCP: what is waiting on the owner. Read only.

"What needs a follow-up this week?" had no tool to call. The tracking API
holds the tasks and the contacts, the cadence report holds the applications
nobody has answered, and an assistant reached neither.

Driven the way `tests/test_mcp.py` drives the rest: tool, HTTP client, the real
FastAPI app, Postgres.
"""

from __future__ import annotations

import inspect
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from apps.mcp import server as mcp_server
from packages.core.models import Application
from packages.tracking.tasks import ensure_task_for_outcome

# The binding to the ASGI app is the other file's, so the two cannot differ.
from tests.test_mcp import _bind_tools_to_app, call  # noqa: F401


async def _submitted(worker_session, candidate: dict[str, str], job: int, days_ago: int = 0) -> str:
    application = Application(
        candidate_id=uuid.UUID(candidate["candidate_id"]),
        profile_id=uuid.UUID(candidate["profile_id"]),
        url=f"https://boards.greenhouse.io/acme/jobs/{job}",
        status="submitted",
        created_at=datetime.now(UTC) - timedelta(days=days_ago),
    )
    worker_session.add(application)
    await worker_session.commit()
    return str(application.id)


@pytest.fixture
async def app_id(worker_session, complete_candidate) -> str:
    return await _submitted(worker_session, complete_candidate, job=77)


async def _task(client: AsyncClient, app_id: str, **body: Any) -> dict[str, Any]:
    payload = {"kind": "interview", "title": "Onsite", **body}
    response = await client.post(f"/applications/{app_id}/tasks", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _in(days: int) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _titles(tasks: list[dict[str, Any]]) -> list[str]:
    return [task["title"] for task in tasks]


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------


async def test_open_tasks_are_grouped_by_when_they_fall(client, app_id) -> None:
    await _task(client, app_id, title="Late", due_at=_in(-1))
    await _task(client, app_id, title="Soon", due_at=_in(2))
    await _task(client, app_id, title="Far", due_at=_in(30))
    done = await _task(client, app_id, title="Done", due_at=_in(1))
    await client.patch(f"/tasks/{done['id']}", json={"completed": True})

    waiting = await call("follow_ups")

    assert _titles(waiting["overdue"]) == ["Late"]
    assert _titles(waiting["due"]) == ["Soon"]
    assert waiting["later"] == 1
    assert waiting["within_days"] == 7
    assert "Done" not in json.dumps(waiting)


async def test_the_window_can_be_widened(client, app_id) -> None:
    await _task(client, app_id, title="Far", due_at=_in(30))

    waiting = await call("follow_ups", within_days=45)

    assert _titles(waiting["due"]) == ["Far"]
    assert waiting["later"] == 0


async def test_a_task_with_no_date_is_listed(client, app_id, worker_session) -> None:
    """A recruiter's reply that says "interview" creates a task with no date:
    the date is in their message, and reading it out of prose would be a guess
    about a deadline (`packages/tracking/tasks.py`). So the task that most
    needs the owner is the one a list of what is due this week leaves out."""
    application = await worker_session.get(Application, uuid.UUID(app_id))
    await ensure_task_for_outcome(worker_session, application, "interview")
    await worker_session.commit()

    waiting = await call("follow_ups")

    [task] = waiting["undated"]
    assert task["kind"] == "interview" and task["source"] == "inbox"
    assert task["due_at"] is None
    assert waiting["overdue"] == [] and waiting["due"] == []


async def test_a_listed_task_is_cut_to_what_a_list_needs(client, app_id) -> None:
    created = await _task(
        client,
        app_id,
        title="Onsite",
        due_at=_in(2),
        location="https://meet.example/abc",
        notes="Bring the portfolio. " * 40,
    )
    ticked = [{**item, "done": index == 0} for index, item in enumerate(created["checklist"])]
    await client.patch(f"/tasks/{created['id']}", json={"checklist": ticked})

    [task] = (await call("follow_ups"))["due"]

    # The exact set, so adding a field is a deliberate act.
    assert set(task) == {
        "id",
        "application_id",
        "application_url",
        "kind",
        "title",
        "due_at",
        "overdue",
        "source",
        "checklist_left",
    }
    assert task["application_url"].endswith("/jobs/77")
    assert task["checklist_left"] == [item["text"] for item in created["checklist"][1:]]
    assert len(json.dumps(task)) < 700, "one task should not cost a model a page"


# --------------------------------------------------------------------------
# Applications nobody has answered
# --------------------------------------------------------------------------


async def test_an_unanswered_application_is_listed_until_it_is_stale(
    client, worker_session, complete_candidate
) -> None:
    quiet = await _submitted(worker_session, complete_candidate, job=1, days_ago=30)
    await _submitted(worker_session, complete_candidate, job=2, days_ago=60)
    await _submitted(worker_session, complete_candidate, job=3, days_ago=2)

    waiting = await call("follow_ups")

    [silent] = waiting["silent"]
    assert silent == {
        "application_id": quiet,
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "days_since": 30,
        "has_follow_up_task": False,
    }
    assert waiting["stale"] == 1


async def test_one_the_owner_already_has_in_hand_says_so(
    client, worker_session, complete_candidate
) -> None:
    quiet = await _submitted(worker_session, complete_candidate, job=1, days_ago=30)
    await _task(client, quiet, kind="follow_up", title="Write to the recruiter")

    waiting = await call("follow_ups")

    assert waiting["silent"][0]["has_follow_up_task"] is True
    assert _titles(waiting["undated"]) == ["Write to the recruiter"]


async def test_the_threshold_can_be_moved(client, worker_session, complete_candidate) -> None:
    await _submitted(worker_session, complete_candidate, job=1, days_ago=5)

    assert (await call("follow_ups"))["silent"] == []
    assert len((await call("follow_ups", silent_after_days=3))["silent"]) == 1


async def test_nothing_waiting_says_what_nothing_can_mean() -> None:
    """Against the real API and an empty database."""
    waiting = await call("follow_ups")

    assert waiting["overdue"] == waiting["due"] == waiting["undated"] == waiting["silent"] == []
    assert waiting["later"] == 0 and waiting["stale"] == 0
    assert waiting["suggested_silent_after_days"] is None
    assert "submitted" in waiting["note"]


@pytest.mark.parametrize("asked", [{"within_days": 400}, {"silent_after_days": 0}])
async def test_a_number_the_api_refuses_is_refused_not_ignored(asked) -> None:
    waiting = await call("follow_ups", **asked)

    assert waiting["code"] == "invalid_request"
    assert "overdue" not in waiting


# --------------------------------------------------------------------------
# One application
# --------------------------------------------------------------------------


async def test_a_contact_is_named_and_their_details_stay_here(client, app_id) -> None:
    """A contact is somebody else. Over MCP the reader is a model that is not
    on this machine, and they never chose that. Who they are is sent; how to
    reach them, and what the owner wrote about them, is not."""
    linked = await client.post(
        f"/applications/{app_id}/contacts",
        json={
            "relationship": "hiring_manager",
            "contact": {
                "name": "Ada Recruiter",
                "email": "ada@acme.example",
                "phone": "+1 415 555 0100",
                "company": "Acme",
                "role": "Talent",
                "profile_url": "https://example.test/in/ada",
                "notes": "Said the band tops out at 210.",
            },
        },
    )
    assert linked.status_code == 200, linked.text

    tracking = await call("application_tracking", application_id=app_id)

    # The exact set, so adding a field is a deliberate act.
    assert tracking["contacts"] == [
        {
            "name": "Ada Recruiter",
            "relationship": "hiring_manager",
            "company": "Acme",
            "role": "Talent",
        }
    ]
    sent = json.dumps(tracking)
    for kept_here in ("ada@acme.example", "555 0100", "example.test/in/ada", "210"):
        assert kept_here not in sent


async def test_one_applications_tasks_come_whole(client, app_id) -> None:
    await _task(
        client,
        app_id,
        due_at=_in(2),
        location="https://meet.example/abc",
        notes="Ask about on-call",
    )

    tracking = await call("application_tracking", application_id=app_id)

    [task] = tracking["tasks"]
    assert task["location"] == "https://meet.example/abc"
    assert task["notes"] == "Ask about on-call"
    assert len(task["checklist"]) == 5


async def test_an_application_that_does_not_exist_is_not_found() -> None:
    tracking = await call("application_tracking", application_id=str(uuid.uuid4()))

    assert tracking["code"] == "not_found"


# --------------------------------------------------------------------------
# Read only
# --------------------------------------------------------------------------


class _Recording:
    """Stands in for the API: what was asked of it, and a reply of the right shape."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str, dict[str, Any]]] = []

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.asked.append((method, path, kwargs.get("params") or {}))
        if path == "/tasks":
            return []
        if path == "/analytics/cadence":
            return {"silent": [], "due": 0, "stale": 0, "latency": {}}
        return {"contacts": [], "tasks": []}


async def test_the_tracker_tools_read_and_do_not_write(monkeypatch) -> None:
    """Nothing here adds a task, ticks one off or links a contact. The record
    of who the owner is in touch with stays the owner's to keep."""
    api = _Recording()
    monkeypatch.setattr(mcp_server, "_client", api)

    await call("follow_ups", within_days=10, silent_after_days=21)
    await call("application_tracking", application_id="a-1")

    assert {method for method, _, _ in api.asked} == {"GET"}
    assert {path for _, path, _ in api.asked} == {
        "/tasks",
        "/analytics/cadence",
        "/applications/a-1/tracking",
    }


async def test_every_parameter_sent_is_one_its_route_reads(monkeypatch) -> None:
    """FastAPI ignores a query parameter it does not know. A window spelt
    wrong here would be sent, dropped, and answered as if nobody had asked."""
    from apps.api.routers import analytics, tracking

    api = _Recording()
    monkeypatch.setattr(mcp_server, "_client", api)

    await call("follow_ups", within_days=10, silent_after_days=21)

    reads = {
        "/tasks": set(inspect.signature(tracking.upcoming_tasks).parameters),
        "/analytics/cadence": set(inspect.signature(analytics.read_cadence).parameters),
    }
    sent = [(path, set(params)) for _, path, params in api.asked]
    assert any(params for _, params in sent), "the window was never sent"
    for path, params in sent:
        assert params <= reads[path], (path, params - reads[path])


async def test_the_threshold_is_the_reports_own_unless_one_is_given(monkeypatch) -> None:
    api = _Recording()
    monkeypatch.setattr(mcp_server, "_client", api)

    await call("follow_ups")

    [cadence] = [params for _, path, params in api.asked if path == "/analytics/cadence"]
    assert cadence == {}
