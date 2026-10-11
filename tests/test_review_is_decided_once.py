"""An application parked for the owner is released once, however it is asked.

`POST /applications/{id}/review` read the application, saw `needs_review`,
moved it to `running` and queued the run. Two requests at once are two
transactions, and each read the row before the other had committed: both saw
`needs_review`, both moved it, and both queued a run. The same for a code
sent twice to `/otp`.

Two `apply` tasks for one application is one run with a single worker, since
the second finds the application finished. With `make workers n=4` it is two
runs at once on one form, each holding the lease on its own task, and
`begin_work` takes a `running` row as an abandoned run to resume. The form
goes to the employer twice.

Found on 2026-10-10 by reading every producer of a queue task for the look
and then the enqueue that `request_crawl` had. A doubled click is enough, and
so is a tool call made again while the first is still being answered.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from apps.api.errors import ApiError
from apps.api.routers.applications import APPLY_TASK_KIND, review_application, submit_otp
from packages.core.enums import ErrorCode
from packages.core.models import Application, ApplicationEvent, QueueTask
from packages.core.schemas import OtpSubmission, ReviewDecision
from tests.test_chat_skill_gaps import _owner


async def _parked(maker, status: str) -> Application:
    async with maker() as session:
        profile, _ = await _owner(session)
        application = Application(
            candidate_id=profile.candidate_id,
            profile_id=profile.id,
            url="https://boards.greenhouse.io/acme/jobs/41",
            ats="greenhouse",
            status=status,
        )
        session.add(application)
        await session.commit()
        return application


async def _runs_queued(maker) -> int:
    async with maker() as session:
        return await session.scalar(
            select(func.count()).select_from(QueueTask).where(QueueTask.kind == APPLY_TASK_KIND)
        )


async def _moves_to_running(maker, application: Application) -> int:
    async with maker() as session:
        events = await session.scalars(
            select(ApplicationEvent).where(ApplicationEvent.application_id == application.id)
        )
        return sum(1 for event in events.all() if (event.payload_json or {}).get("to") == "running")


async def _decide(maker, decide) -> str:
    """One caller, on a session of its own. What it was told."""
    async with maker() as session:
        try:
            await decide(session)
        except ApiError as refused:
            return refused.code.value if hasattr(refused.code, "value") else str(refused.code)
        return "done"


async def test_two_approvals_at_once_release_one_run(committing_sessionmaker) -> None:
    application = await _parked(committing_sessionmaker, "needs_review")

    def approve(session):
        return review_application(application.id, ReviewDecision(approve=True), session)

    told = await asyncio.gather(*(_decide(committing_sessionmaker, approve) for _ in range(3)))

    assert sorted(told) == ["done", ErrorCode.INVALID_STATE.value, ErrorCode.INVALID_STATE.value]
    assert await _runs_queued(committing_sessionmaker) == 1


async def test_an_approval_and_a_rejection_at_once_are_one_decision(
    committing_sessionmaker,
) -> None:
    """Whichever is first stands, and the other is told the application has moved on."""
    application = await _parked(committing_sessionmaker, "needs_review")

    def approve(session):
        return review_application(application.id, ReviewDecision(approve=True), session)

    def reject(session):
        return review_application(application.id, ReviewDecision(approve=False), session)

    told = await asyncio.gather(
        _decide(committing_sessionmaker, approve), _decide(committing_sessionmaker, reject)
    )

    assert sorted(told) == ["done", ErrorCode.INVALID_STATE.value]
    async with committing_sessionmaker() as session:
        final = (await session.get(Application, application.id)).status
    assert await _runs_queued(committing_sessionmaker) == (1 if final == "running" else 0)


async def test_a_code_sent_twice_at_once_resumes_one_run(committing_sessionmaker) -> None:
    application = await _parked(committing_sessionmaker, "needs_otp")

    def send(session):
        return submit_otp(application.id, OtpSubmission(code="123456"), session)

    told = await asyncio.gather(*(_decide(committing_sessionmaker, send) for _ in range(3)))

    assert sorted(told) == ["done", ErrorCode.INVALID_STATE.value, ErrorCode.INVALID_STATE.value]
    assert await _runs_queued(committing_sessionmaker) == 1


@pytest.mark.parametrize("approve", [True, False])
async def test_one_decision_on_its_own_is_as_it_was(committing_sessionmaker, approve) -> None:
    application = await _parked(committing_sessionmaker, "needs_review")

    async with committing_sessionmaker() as session:
        decided = await review_application(application.id, ReviewDecision(approve=approve), session)

    assert decided.status == ("running" if approve else "failed")
    assert await _runs_queued(committing_sessionmaker) == (1 if approve else 0)
