"""The apply pipeline.

Phase 0 is a stub: it claims the application, sleeps, and submits. Phase 1
replaces the middle with Playwright — `parse_posting` → `enumerate_fields` →
`fill` → screenshot → `submit` — and the surrounding lease/idempotency
handling stays exactly as it is here.
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config import get_settings
from packages.core.enums import ApplicationStatus, FailureReason
from packages.core.models import Application
from packages.core.queue import ClaimedTask
from packages.core.state import WorkClaim, begin_work, transition

log = structlog.get_logger(__name__)

#: Phase 0 stand-in for the browser run.
STUB_WORK_SECONDS = 2.0


class TaskPayloadError(Exception):
    """The task payload does not name a workable application."""


async def handle_apply(session: AsyncSession, claimed: ClaimedTask) -> None:
    """Run one apply task. Caller holds the lease and owns the transaction.

    Idempotent: safe to run again after a crash at any point.
    """
    raw_id = claimed.task.payload_json.get("application_id")
    if not raw_id:
        raise TaskPayloadError("payload has no application_id")

    application = await session.get(Application, raw_id)
    if application is None:
        raise TaskPayloadError(f"application {raw_id} does not exist")

    if claimed.reclaimed:
        # Distinguishing these matters: our own dead lease means we know
        # nothing else ever touched the row. Either way the lease has expired,
        # so the task is genuinely unowned and safe to take.
        log.info(
            "reclaimed_expired_lease",
            application_id=str(application.id),
            previous_owner=claimed.previous_owner,
            own_lease=claimed.reclaimed_from_self,
            attempts=claimed.task.attempts,
        )

    claim = await begin_work(session, application)

    if claim is WorkClaim.ALREADY_DONE:
        log.info(
            "application_already_terminal",
            application_id=str(application.id),
            status=application.status,
        )
        return

    if claim is WorkClaim.RESUMED:
        log.info("resuming_abandoned_run", application_id=str(application.id))

    await _run_pipeline(session, application)


async def _run_pipeline(session: AsyncSession, application: Application) -> None:
    """Phase 0 stub. Phase 1 puts the browser here."""
    await asyncio.sleep(STUB_WORK_SECONDS)

    settings = get_settings()

    # The approval gate. AUTO_SUBMIT is false by default and auto-submit is
    # additionally opt-in per profile, so the default path always parks for a
    # human. CLAUDE.md §2.3.
    if not settings.auto_submit:
        await transition(
            session,
            application,
            ApplicationStatus.NEEDS_REVIEW,
            payload={"reason": "auto_submit disabled; awaiting owner approval"},
        )
        return

    await transition(session, application, ApplicationStatus.SUBMITTED)


async def park_failed(
    session: AsyncSession,
    application: Application,
    reason: FailureReason,
    message: str,
) -> None:
    """Move an application to failed, if it is still in a state that allows it."""
    status = ApplicationStatus(application.status)
    if status in (ApplicationStatus.SUBMITTED, ApplicationStatus.FAILED):
        return
    if status is not ApplicationStatus.RUNNING:
        await begin_work(session, application)
    await transition(
        session,
        application,
        ApplicationStatus.FAILED,
        failure_reason=reason,
        payload={"message": message},
    )
