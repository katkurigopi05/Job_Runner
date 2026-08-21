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
from sqlalchemy import select
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
from packages.ats.screen import ScreenReport, screen
from packages.core.config import get_settings
from packages.core.enums import ApplicationStatus, FailureReason
from packages.core.models import Application, Candidate, Match, Profile, Resume
from packages.core.queue import ClaimedTask
from packages.core.state import WorkClaim, begin_work, transition
from packages.core.storage import get_storage, receipt_key
from packages.llm import router as llm_router

log = structlog.get_logger(__name__)


async def _match_score(session: AsyncSession, application: Application) -> float | None:
    """This profile's score for the posting, or None if it was never scored.

    An application created straight from a URL has no posting attached, so
    there is nothing to have scored. Callers must treat None as "did not clear
    the threshold" rather than substituting a default.
    """
    if application.posting_id is None:
        return None
    return await session.scalar(
        select(Match.score).where(
            Match.profile_id == application.profile_id,
            Match.posting_id == application.posting_id,
        )
    )


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

        # Read the form before answering any of it. A question whose honest
        # answer ends the application, or one an employer should not be
        # asking, is worth surfacing while the decision is still the owner's
        # — not after a fill, a screenshot and a rejection.
        screening = screen(questions, profile)

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

        # §9 Phase 3 — the diff has to be on the screen before the owner
        # approves, not after. Computed here so the review record carries it,
        # and the tailored PDF is rendered from the same vetted result so the
        # file the owner uploads is the document the diff described.
        diff = await _tailor(session, application, profile, posting.description_raw or "")

        await _decide(
            session, application, profile, report, adapter, page, diff, screening=screening
        )


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


async def _prepared_resume(
    session: AsyncSession, application: Application, profile: Profile
) -> Any | None:
    """The résumé a batch already tailored for this posting, if there is one.

    Returns None rather than raising when the stored file has gone missing —
    the run then tailors normally, which is slower and correct.
    """
    if application.posting_id is None:
        return None

    match = await session.scalar(
        select(Match).where(
            Match.profile_id == profile.id,
            Match.posting_id == application.posting_id,
            Match.tailored_resume_id.is_not(None),
        )
    )
    if match is None or match.tailored_resume_id is None:
        return None

    resume = await session.get(Resume, match.tailored_resume_id)
    if resume is None:
        return None
    if not get_storage().path_for(resume.storage_ref).is_file():
        log.warning("prepared_resume_missing", storage_ref=resume.storage_ref)
        return None
    return resume.id


async def _tailor(
    session: AsyncSession,
    application: Application,
    profile: Profile,
    posting_text: str,
) -> dict[str, Any] | None:
    """Tailor the base résumé for this posting, render it, and return the diff.

    Phase 3 built the rewriter, the fabrication guard, and the diff, and
    nothing ever called them — tailored_resume_id was always null, so the
    review screen had nothing to show. This is the call that makes a diff
    exist.

    Rendering happens here, off the same `TailorResult` the diff is computed
    from, because those two must not be able to disagree. Tailoring twice
    would be non-deterministic on any real provider, and the owner would be
    reading one document and uploading another.

    §2.1 is enforced inside tailor_bullets: every rewrite is guard-checked
    against the source, and one that introduces an unsupported entity falls
    back to the original line. `rejected` counts those, and the review screen
    shows it — a guard that silently substituted would be indistinguishable
    from a guard that never fired.

    Returns None when there is nothing to tailor. A failure here must not stop
    the application: the untailored résumé is still a correct résumé.
    """
    if profile.base_resume_id is None or not posting_text.strip():
        return None

    # A batch run may already have tailored this posting overnight
    # (packages/tailor/batch.py). Reusing it is the whole point: it takes the
    # slowest step out of the critical path, and re-tailoring would spend a
    # second set of provider calls to produce a *different* document from the
    # one the owner may already have reviewed.
    prepared = await _prepared_resume(session, application, profile)
    if prepared is not None:
        application.tailored_resume_id = prepared
        return {"reused": True}

    resume = await session.get(Resume, profile.base_resume_id)
    if resume is None or not resume.parsed_json:
        return None

    try:
        from packages.tailor.diff import summarize
        from packages.tailor.guard import SourceCorpus
        from packages.tailor.parse import ParsedResume
        from packages.tailor.rewrite import tailor_bullets

        parsed = ParsedResume.model_validate(resume.parsed_json)
        bullets = [line for line in parsed.section("experience") if line.strip()]
        if not bullets:
            return None

        # from_resume, not from_texts: the guard has to be able to tell one
        # employer's entry from another's, or a rewrite can move a metric
        # between them and still trace to "the résumé".
        corpus = SourceCorpus.from_resume(parsed)
        provider = llm_router.tailor_resume()
        result = await tailor_bullets(provider, bullets, posting_text, corpus)
        summary = summarize(result)

        # The file the owner uploads. Rendered even when every rewrite was
        # rejected: that document is the source résumé with the current
        # Projects section rebuilt into it, which is still the right thing to
        # send and is not what sits in storage as the base.
        from packages.tailor.publish import publish_tailored

        published = await publish_tailored(
            session,
            candidate_id=resume.candidate_id,
            parsed=parsed,
            result=result,
        )
        if published is not None:
            application.tailored_resume_id = published.id

        return {
            "changed": summary.changed,
            "unchanged": summary.unchanged,
            # Rewrites the guard refused. Surfaced, not swallowed.
            "rejected": summary.rejected,
            "unified": summary.unified,
            "changes": [change.model_dump() for change in summary.changes],
        }
    except Exception as exc:  # noqa: BLE001 - tailoring is an enhancement
        log.warning("tailoring_failed", error=type(exc).__name__)
        return None


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
    resume_diff: dict[str, Any] | None = None,
    screening: ScreenReport | None = None,
) -> None:
    """Park for review, or submit — the approval gate.

    Three independent conditions must all hold before anything is sent:
    every required question is answered, AUTO_SUBMIT is on, and this profile
    opted in. Otherwise the run stops with the form filled and screenshotted.

    A knock-out finding stops the *unattended* path as a fourth condition —
    see below. It never overrides the owner's own approval.
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
        "resume_diff": resume_diff,
        # Read off the form before anything was answered.
        "screening": screening.as_dict() if screening else None,
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

    # The owner already looked at this form and said yes. That approval *is*
    # the authorization CLAUDE.md §2.3 asks for, so it stands on its own —
    # AUTO_SUBMIT and the score threshold govern submitting *without* a human,
    # and re-applying them here would park an approved application forever.
    if (application.review_json or {}).get("owner_approved"):
        await _submit(session, application, adapter, page)
        return

    # A knock-out is a question whose honest answer from this profile likely
    # disqualifies. Sending anyway costs the owner nothing and a recruiter
    # their time, which is the spray behaviour this project exists not to do.
    # So it parks — but only on the unattended path. The owner having already
    # approved this form outranks it, and that branch is above.
    if screening is not None and screening.knock_outs:
        await transition(
            session,
            application,
            ApplicationStatus.NEEDS_REVIEW,
            payload={
                "reason": "the form asks a question this profile likely fails",
                "questions": [q.label for q in screening.knock_outs],
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

    # §2.3 makes the threshold part of the opt-in, not advice: auto-submit is
    # permitted "above a match-score threshold you choose". A profile carrying
    # min_match_score=0.9 must not submit to something scored 0.2 — and must
    # not submit to something never scored at all, which is every application
    # until matching lands in Phase 5. No score is not a passing score.
    score = await _match_score(session, application)
    if score is None or score < profile.min_match_score:
        await transition(
            session,
            application,
            ApplicationStatus.NEEDS_REVIEW,
            payload={
                "reason": (
                    "auto-submit is on but this posting has no match score yet"
                    if score is None
                    else "match score is below the profile's threshold"
                ),
                "score": score,
                "min_match_score": profile.min_match_score,
            },
        )
        return

    await _submit(session, application, adapter, page)


async def _submit(
    session: AsyncSession,
    application: Application,
    adapter: Any,
    page: Any,
) -> None:
    """Send the form and record the receipt. The only path that submits."""
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
