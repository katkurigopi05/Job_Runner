"""Recording an application the owner sent by hand.

§2.5 makes this permanent, not temporary: every supported ATS mounts a
captcha on the apply form, this project will not work around one, and so the
final click is always the owner's. Until now there was nowhere to record that
they made it — a finished application sat on the board as `needs_review` or
`failed[manual_completion_required]` forever, so the pipeline lied and the
funnel counted a sent application as a failure.

The risk in adding these edges is resurrection: `submitted` and `failed` are
terminal precisely so a redelivered queue task cannot re-run a finished
application. That property has to survive, and the way it survives is that the
worker never takes these edges — only an explicit owner request does.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from packages.core.enums import ApplicationStatus, FailureReason
from packages.core.state import ALLOWED_TRANSITIONS, OWNER_ONLY_TRANSITIONS, can_transition


def test_submitted_remains_terminal() -> None:
    """Nothing leaves `submitted`. That is what makes redelivery safe."""
    assert ALLOWED_TRANSITIONS[ApplicationStatus.SUBMITTED] == frozenset()


def test_failed_may_only_become_submitted() -> None:
    """One edge out, not a general reopening.

    `failed -> running` would let a redelivered task re-run an application the
    machine already gave up on, which is the loop terminality exists to stop.
    """
    assert ALLOWED_TRANSITIONS[ApplicationStatus.FAILED] == frozenset({ApplicationStatus.SUBMITTED})
    assert not can_transition(ApplicationStatus.FAILED, ApplicationStatus.RUNNING)


def test_the_owner_only_edges_are_named() -> None:
    """Declared rather than implied, so the worker's paths stay auditable."""
    assert (
        frozenset(
            {
                (ApplicationStatus.NEEDS_REVIEW, ApplicationStatus.SUBMITTED),
                (ApplicationStatus.FAILED, ApplicationStatus.SUBMITTED),
            }
        )
        == OWNER_ONLY_TRANSITIONS
    )


async def _application(client: AsyncClient, candidate: dict[str, str], url: str) -> str:
    created = await client.post(
        "/applications",
        json={
            "candidate_id": candidate["candidate_id"],
            "profile_id": candidate["profile_id"],
            "url": url,
        },
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


@pytest.mark.asyncio
async def test_a_blocked_application_can_be_recorded_as_sent(
    client: AsyncClient, complete_candidate: dict[str, str], worker_session
) -> None:
    """The captcha case, end to end.

    This is the single most common outcome of a real run, and before this it
    was unrecordable.
    """
    from packages.core.models import Application

    application_id = await _application(
        client, complete_candidate, "https://boards.greenhouse.io/acme/jobs/900"
    )
    application = await worker_session.get(Application, uuid.UUID(application_id))
    application.status = ApplicationStatus.FAILED.value
    application.failure_reason = FailureReason.MANUAL_COMPLETION_REQUIRED.value
    await worker_session.commit()

    response = await client.post(f"/applications/{application_id}/submitted", json={})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "submitted"
    # The reason it needed a person is history now, not current state.
    assert body["failure_reason"] is None


@pytest.mark.asyncio
async def test_recording_it_twice_is_not_an_error(
    client: AsyncClient, complete_candidate: dict[str, str], worker_session
) -> None:
    """At a hundred a day the owner will double-tap.

    A queue that punishes that is a queue that gets used slowly, which defeats
    the only purpose this screen has.
    """
    from packages.core.models import Application

    application_id = await _application(
        client, complete_candidate, "https://boards.greenhouse.io/acme/jobs/901"
    )
    application = await worker_session.get(Application, uuid.UUID(application_id))
    application.status = ApplicationStatus.NEEDS_REVIEW.value
    await worker_session.commit()

    first = await client.post(f"/applications/{application_id}/submitted", json={})
    second = await client.post(f"/applications/{application_id}/submitted", json={})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "submitted"


@pytest.mark.asyncio
async def test_the_event_records_what_it_was_before(
    client: AsyncClient, complete_candidate: dict[str, str], worker_session
) -> None:
    """Why a person was needed is the interesting part of the history."""
    from packages.core.models import Application

    application_id = await _application(
        client, complete_candidate, "https://boards.greenhouse.io/acme/jobs/902"
    )
    application = await worker_session.get(Application, uuid.UUID(application_id))
    application.status = ApplicationStatus.FAILED.value
    application.failure_reason = FailureReason.MANUAL_COMPLETION_REQUIRED.value
    await worker_session.commit()

    await client.post(f"/applications/{application_id}/submitted", json={"note": "solved captcha"})
    events = (await client.get(f"/applications/{application_id}/events")).json()

    last = events[-1]
    assert last["payload"]["by"] == "owner"
    assert last["payload"]["was"] == "failed"
    assert last["payload"]["failure_reason"] == "manual_completion_required"


@pytest.mark.asyncio
async def test_the_manual_queue_excludes_failures_a_person_cannot_finish(
    client: AsyncClient, complete_candidate: dict[str, str], worker_session
) -> None:
    """A closed job is not work waiting on the owner.

    Putting it in the queue would spend the scarcest thing here — attention —
    on an application that cannot be completed at all.
    """
    from packages.core.models import Application

    closed_id = await _application(
        client, complete_candidate, "https://boards.greenhouse.io/acme/jobs/903"
    )
    closed = await worker_session.get(Application, uuid.UUID(closed_id))
    closed.status = ApplicationStatus.FAILED.value
    closed.failure_reason = FailureReason.JOB_CLOSED.value
    await worker_session.commit()

    queue = (await client.get("/applications/queue/manual")).json()

    assert closed_id not in [packet["application_id"] for packet in queue]
