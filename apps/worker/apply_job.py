"""The apply pipeline.

parse_posting → enumerate_fields → fill → screenshot → (approval) → submit.

The shape of the run is fixed by two rules:

- Nothing submits without approval unless AUTO_SUBMIT is on *and* the profile
  opts in above its match threshold (CLAUDE.md §2.3).
- Any required question the agent cannot answer parks the application at
  `needs_review` carrying the question's exact text (CLAUDE.md §2.4).
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from apps.worker.browser import browser_page
from packages.ats.answers import build_answers
from packages.ats.base import (
    FillReport,
    ManualCompletionRequired,
    SiteError,
    UnsupportedSiteError,
)
from packages.ats.registry import adapter_for
from packages.core.config import get_settings
from packages.core.enums import ApplicationStatus, FailureReason
from packages.core.models import Application, Candidate, Profile, Resume
from packages.core.queue import ClaimedTask
from packages.core.state import WorkClaim, begin_work, transition
from packages.core.storage import get_storage, receipt_key

log = structlog.get_logger(__name__)


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

    candidate = await session.get(Candidate, application.candidate_id)
    profile = await session.get(Profile, application.profile_id)
    if candidate is None or profile is None:  # pragma: no cover - FK guarantees
        raise TaskPayloadError("application is missing its candidate or profile")

    try:
        await _run_pipeline(session, application, candidate, profile)
    except UnsupportedSiteError as exc:
        await _fail(session, application, FailureReason.UNSUPPORTED_SITE, str(exc))
    except ManualCompletionRequired as exc:
        # A blocked site is a scope boundary, not a bug to work around.
        await _fail(session, application, FailureReason.MANUAL_COMPLETION_REQUIRED, str(exc))
    except SiteError as exc:
        await _fail(session, application, FailureReason.SITE_ERROR, str(exc))


async def _run_pipeline(
    session: AsyncSession,
    application: Application,
    candidate: Candidate,
    profile: Profile,
) -> None:
    adapter = adapter_for(application.url)
    application.ats = adapter.name

    async with browser_page(adapter.name) as page:
        await page.goto(application.url, wait_until="domcontentloaded")

        posting = await adapter.parse_posting(page)
        if posting.closed:
            await _fail(session, application, FailureReason.JOB_CLOSED, "posting is closed")
            return

        questions = await adapter.enumerate_fields(page)

        # Answers the owner supplied at review, if this is a resumed run.
        review = application.review_json or {}
        owner_answers = review.get("owner_answers") or {}

        answers = build_answers(
            questions,
            candidate,
            profile,
            extra=owner_answers,
            resume_path=await _resume_path(session, profile),
        )
        report = await adapter.fill(page, answers)

        report.screenshot_ref = await _capture(page, application, "filled-form.png")

        await _decide(session, application, profile, report, adapter, page)


async def _resume_path(session: AsyncSession, profile: Profile) -> str | None:
    """Absolute path of the profile's base résumé, for the file upload field.

    Returns None if there is no résumé or the stored file has gone missing —
    the required résumé field then goes to the owner unanswered rather than
    the run failing with a stack trace.
    """
    if profile.base_resume_id is None:
        return None

    resume = await session.get(Resume, profile.base_resume_id)
    if resume is None:
        return None

    path = get_storage().path_for(resume.storage_ref)
    if not path.is_file():
        log.warning("resume_file_missing", storage_ref=resume.storage_ref)
        return None
    return str(path)


async def _capture(page: Any, application: Application, name: str) -> str | None:
    """Screenshot into storage. A failed capture must not fail the run.

    A full-page shot of a long posting can be very large, so an oversized one
    is retried at viewport size rather than discarded — a smaller receipt is
    worth more than none.
    """
    try:
        key = receipt_key(str(application.id), name)
        storage = get_storage()
        target = storage.path_for(key)
        target.parent.mkdir(parents=True, exist_ok=True)

        await page.screenshot(path=str(target), full_page=True)

        enforce = getattr(storage, "enforce_limit", None)
        if enforce is not None and not enforce(key):
            log.warning("screenshot_over_limit_retrying_viewport", key=key)
            await page.screenshot(path=str(target), full_page=False)
            if not enforce(key):
                log.warning("screenshot_still_over_limit_discarded", key=key)
                return None

        return key
    except Exception as exc:  # noqa: BLE001 - the audit artifact is best-effort
        log.warning("screenshot_failed", error=type(exc).__name__)
        return None


async def _decide(
    session: AsyncSession,
    application: Application,
    profile: Profile,
    report: FillReport,
    adapter: Any,
    page: Any,
) -> None:
    """Park for review, or submit — the approval gate.

    Three independent conditions must all hold before anything is sent:
    every required question is answered, AUTO_SUBMIT is on, and this profile
    opted in. Otherwise the run stops with the form filled and screenshotted.
    """
    settings = get_settings()

    application.review_json = {
        **(application.review_json or {}),
        "fill_rate": round(report.fill_rate, 3),
        "filled": [f.model_dump() for f in report.filled],
        "skipped": [s.model_dump() for s in report.skipped],
        # The exact question text, preserved for the owner.
        "unanswered": [q.model_dump() for q in report.unanswered],
        "screenshot_ref": report.screenshot_ref,
    }

    if not report.is_complete:
        await transition(
            session,
            application,
            ApplicationStatus.NEEDS_REVIEW,
            payload={
                "reason": "required questions could not be answered",
                "questions": [q.question for q in report.unanswered if q.required],
            },
        )
        return

    if not (settings.auto_submit and profile.auto_submit):
        await transition(
            session,
            application,
            ApplicationStatus.NEEDS_REVIEW,
            payload={"reason": "awaiting owner approval before submit"},
        )
        return

    receipt = await adapter.submit(page)
    receipt.screenshot_ref = await _capture(page, application, "submitted.png")
    application.receipt_json = receipt.model_dump()

    await transition(
        session,
        application,
        ApplicationStatus.SUBMITTED,
        payload={"confirmation": receipt.confirmation_text},
    )


async def _fail(
    session: AsyncSession,
    application: Application,
    reason: FailureReason,
    message: str,
) -> None:
    await transition(
        session,
        application,
        ApplicationStatus.FAILED,
        failure_reason=reason,
        payload={"message": message[:500]},
    )


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
    await _fail(session, application, reason, message)
