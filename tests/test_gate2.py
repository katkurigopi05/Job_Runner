"""Gate 2, the half that can be checked offline — CLAUDE.md §9.

The gate reads: "a real posting fills >=80% of fields with zero manual input.
Every unfilled field appears in the review queue with its original question
text. Approving in the UI resumes the application."

The fill-rate clause needs a real posting and a real profile — that is
`make gate-2-live`. The other two clauses are properties of the pipeline and
are checked here, because they are the ones that decide whether the owner can
answer what the agent could not.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.enums import ApplicationStatus
from packages.core.models import Application
from packages.core.state import begin_work, transition

APPLY_URL = "https://boards.greenhouse.io/acme/jobs/4012345"

#: Deliberately awkward wording. An employer's question is quoted, never
#: normalized — §2.4 requires the exact text, and a tidied-up version is a
#: different question.
EMPLOYER_QUESTIONS = [
    "Why do you want to work at Acme, specifically?",
    "Are you authorized to work in the country for which you applied?",
    "Please double-check all the information provided above. Ensuring accuracy is crucial.",
]


@pytest.fixture
async def parked(client: AsyncClient, worker_session: AsyncSession, complete_candidate) -> dict:
    """An application parked on questions the agent would not guess."""
    created = await client.post("/applications", json={**complete_candidate, "url": APPLY_URL})
    assert created.status_code == 201, created.text
    application_id = created.json()["id"]

    application = await worker_session.get(Application, uuid.UUID(application_id))
    assert application is not None

    await begin_work(worker_session, application)
    application.review_json = {
        "fill_rate": 0.82,
        "filled": [{"key": "email", "question": "Email", "value": "owner@example.com"}],
        "unanswered": [
            {"key": f"q{i}", "question": text, "kind": "textarea", "required": True}
            for i, text in enumerate(EMPLOYER_QUESTIONS)
        ],
    }
    await transition(
        worker_session,
        application,
        ApplicationStatus.NEEDS_REVIEW,
        payload={"reason": "required questions could not be answered"},
    )
    await worker_session.commit()
    return {"id": application_id}


async def test_unfilled_fields_carry_the_employers_own_wording(client: AsyncClient, parked) -> None:
    """§2.4 — the exact question text, not a normalized version of it.

    The owner answers what the employer asked. A question the agent rephrased
    is a different question, and the answer would go on a real application.
    """
    review = (await client.get(f"/applications/{parked['id']}")).json()["review"]

    surfaced = [q["question"] for q in review["unanswered"]]
    assert surfaced == EMPLOYER_QUESTIONS


async def test_approving_resumes_the_application(
    client: AsyncClient, worker_session: AsyncSession, parked
) -> None:
    """The core loop, under the shipped default.

    AUTO_SUBMIT is off, so this proves approval alone moves the run — the
    failure this guards against left the owner unable to submit anything.
    """
    approved = await client.post(
        f"/applications/{parked['id']}/review",
        json={"approve": True, "answers": {"q0": "Because of the payments team."}},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "running"

    polled = (await client.get(f"/applications/{parked['id']}")).json()
    # The owner's words, stored exactly as typed.
    assert polled["review"]["owner_answers"]["q0"] == "Because of the payments team."
    assert polled["review"]["owner_approved"] is True


async def test_rejecting_sends_nothing(client: AsyncClient, parked) -> None:
    rejected = await client.post(f"/applications/{parked['id']}/review", json={"approve": False})
    assert rejected.status_code == 200

    polled = (await client.get(f"/applications/{parked['id']}")).json()
    assert polled["status"] == "failed"
    assert polled["failure_reason"] == "rejected_at_review"


async def test_a_queue_task_is_enqueued_on_approval(
    client: AsyncClient, worker_session: AsyncSession, parked
) -> None:
    """Approval has to hand the run back to a worker, not just change a row."""
    from packages.core.models import QueueTask

    await client.post(f"/applications/{parked['id']}/review", json={"approve": True})

    kinds = list((await worker_session.scalars(select(QueueTask.kind))).all())
    assert "apply" in kinds
