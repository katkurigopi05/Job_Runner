"""Application routes — create, list, get, review."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from packages.core.completeness import missing_requirements
from packages.core.enums import ApplicationStatus, ErrorCode, FailureReason
from packages.core.models import (
    Application,
    ApplicationEvent,
    Candidate,
    Company,
    Posting,
    Profile,
    Resume,
)
from packages.core.queue import enqueue
from packages.core.schemas import (
    ApplicationCreate,
    ApplicationEventOut,
    ApplicationOut,
    ApplicationPacketOut,
    ManualSubmission,
    OtpSubmission,
    PacketAnswer,
    PacketPosting,
    PacketQuestion,
    PacketResume,
    ReviewDecision,
    TailoringChoice,
)
from packages.core.state import transition
from packages.core.storage import get_storage

router = APIRouter(prefix="/applications", tags=["applications"])

APPLY_TASK_KIND = "apply"


@router.post("", response_model=ApplicationOut, status_code=status.HTTP_201_CREATED)
async def create_application(body: ApplicationCreate, session: SessionDep) -> Application:
    candidate = await session.get(Candidate, body.candidate_id)
    if candidate is None:
        raise ApiError(ErrorCode.INVALID_REQUEST, "candidate_id does not exist")

    profile = await session.get(Profile, body.profile_id)
    if profile is None:
        raise ApiError(ErrorCode.INVALID_REQUEST, "profile_id does not exist")

    if profile.candidate_id != candidate.id:
        raise ApiError(ErrorCode.INVALID_REQUEST, "profile does not belong to candidate")

    # `incomplete_candidate` is knowable here, so it is rejected here. An
    # application that provably cannot be completed is never created — no row,
    # no queued task, nothing to fail later.
    missing = missing_requirements(candidate, profile)
    if missing:
        raise ApiError(
            ErrorCode.INVALID_REQUEST,
            f"profile is incomplete, cannot apply: missing {', '.join(missing)}",
        )

    application = Application(
        candidate_id=body.candidate_id,
        profile_id=body.profile_id,
        url=body.url,
        ats=body.ats,
        status=ApplicationStatus.QUEUED.value,
    )
    session.add(application)

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        constraint = getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
        if constraint == "uq_applications_candidate_url":
            raise ApiError(
                ErrorCode.DUPLICATE_APPLICATION,
                "an application for this candidate and url already exists",
            ) from exc
        raise

    session.add(
        ApplicationEvent(
            application_id=application.id,
            type="created",
            payload_json={"url": body.url, "ats": body.ats},
        )
    )
    # The row and its task commit together — a queued application always has a
    # task, and a task always has a row.
    await enqueue(session, APPLY_TASK_KIND, {"application_id": str(application.id)})

    await session.commit()
    await session.refresh(application)
    return application


@router.get("", response_model=list[ApplicationOut])
async def list_applications(
    session: SessionDep, status_filter: ApplicationStatus | None = None
) -> list[Application]:
    stmt = select(Application).order_by(Application.created_at.desc())
    if status_filter is not None:
        stmt = stmt.where(Application.status == status_filter.value)
    result = await session.scalars(stmt)
    return list(result.all())


@router.get("/{application_id}", response_model=ApplicationOut)
async def get_application(application_id: uuid.UUID, session: SessionDep) -> Application:
    application = await session.get(Application, application_id)
    if application is None:
        raise ApiError(ErrorCode.NOT_FOUND, "application not found")
    return application


@router.get("/{application_id}/events", response_model=list[ApplicationEventOut])
async def get_application_events(
    application_id: uuid.UUID, session: SessionDep
) -> list[ApplicationEvent]:
    application = await session.get(Application, application_id)
    if application is None:
        raise ApiError(ErrorCode.NOT_FOUND, "application not found")
    result = await session.scalars(
        select(ApplicationEvent)
        .where(ApplicationEvent.application_id == application_id)
        .order_by(ApplicationEvent.at)
    )
    return list(result.all())


@router.post("/{application_id}/review", response_model=ApplicationOut)
async def review_application(
    application_id: uuid.UUID, body: ReviewDecision, session: SessionDep
) -> Application:
    """Approve or reject a parked application.

    This is the approval gate. Nothing reaches an employer without passing
    through here (or through an explicit per-profile auto-submit above
    threshold). CLAUDE.md §2.3.
    """
    application = await session.get(Application, application_id)
    if application is None:
        raise ApiError(ErrorCode.NOT_FOUND, "application not found")

    if application.status != ApplicationStatus.NEEDS_REVIEW.value:
        raise ApiError(
            ErrorCode.INVALID_STATE,
            f"application is {application.status}, not needs_review",
        )

    if body.approve:
        payload = {"decision": "approve"}
        if body.note:
            payload["note"] = body.note
        # The approval has to be recorded on the application, not only as an
        # event: the resumed run re-enters the submit gate, and without this it
        # cannot tell an owner-approved run from a fresh one. With AUTO_SUBMIT
        # off — the shipped default — it would park again, and approving would
        # never submit anything.
        review = dict(application.review_json or {})
        review["owner_approved"] = True
        # Answers the owner supplied get merged into the review record so the
        # worker can pick them up on resume.
        if body.answers:
            review["owner_answers"] = body.answers
            payload["answered"] = sorted(body.answers)
        application.review_json = review

        await transition(session, application, ApplicationStatus.RUNNING, payload=payload)
        await enqueue(session, APPLY_TASK_KIND, {"application_id": str(application.id)})
    else:
        await transition(
            session,
            application,
            ApplicationStatus.FAILED,
            failure_reason=FailureReason.REJECTED_AT_REVIEW,
            payload={"decision": "reject", "note": body.note} if body.note else None,
        )

    await session.commit()
    await session.refresh(application)
    return application


@router.post("/{application_id}/tailoring/compare", response_model=ApplicationOut)
async def compare_tailoring(application_id: uuid.UUID, session: SessionDep) -> Application:
    """Tailor this posting with the local model and the cloud one, for a choice.

    On demand rather than on every application, and that is a §2.8 decision
    rather than a performance one: each remote side is another upload of the
    owner's résumé to a third party. Running both up front would double that on
    every application, including the ones rejected at review — so it happens
    when the owner asks, and the tailoring cache means asking twice sends
    nothing.

    Both sides are vetted by the fabrication guard before either is shown. A
    comparison offers each column as something the owner may choose and send;
    an unvetted draft presented that way is a fabricated bullet with a button
    under it.
    """
    application = await session.get(Application, application_id)
    if application is None:
        raise ApiError(ErrorCode.NOT_FOUND, "application not found")

    # Parked, not terminal. Re-tailoring something already submitted uploads the
    # résumé again to produce a document that cannot be sent.
    if application.status != ApplicationStatus.NEEDS_REVIEW.value:
        raise ApiError(
            ErrorCode.INVALID_STATE,
            f"application is {application.status}, not needs_review",
        )

    profile = await session.get(Profile, application.profile_id)
    if profile is None:
        raise ApiError(ErrorCode.NOT_FOUND, "profile not found")

    posting = await session.get(Posting, application.posting_id) if application.posting_id else None
    if posting is None:
        raise ApiError(
            ErrorCode.INVALID_REQUEST,
            "this application has no posting on record, so there is no job description "
            "to tailor against",
        )

    from packages.tailor.compare import CannotCompare, compare_tailorings

    try:
        candidates = await compare_tailorings(session, profile=profile, posting=posting)
    except CannotCompare as exc:
        raise ApiError(ErrorCode.INVALID_REQUEST, str(exc)) from exc

    application.review_json = {
        **(application.review_json or {}),
        "tailoring_comparison": [candidate.as_dict() for candidate in candidates],
    }
    await session.commit()
    await session.refresh(application)
    return application


@router.post("/{application_id}/tailoring/select", response_model=ApplicationOut)
async def select_tailoring(
    application_id: uuid.UUID, body: TailoringChoice, session: SessionDep
) -> Application:
    """Send this one. Sets the résumé the apply run will upload.

    The id is checked against the stored comparison rather than merely against
    the candidate's résumés. This decides the file that reaches an employer, and
    the screen only ever offers two — accepting any id the candidate happens to
    own would be a wider door than the feature needs.
    """
    application = await session.get(Application, application_id)
    if application is None:
        raise ApiError(ErrorCode.NOT_FOUND, "application not found")

    review = application.review_json or {}
    offered = {
        candidate.get("resume_id")
        for candidate in review.get("tailoring_comparison") or []
        if candidate.get("resume_id")
    }
    if str(body.resume_id) not in offered:
        raise ApiError(
            ErrorCode.INVALID_REQUEST,
            "that résumé is not one of the compared versions for this application",
        )

    application.tailored_resume_id = body.resume_id
    session.add(
        ApplicationEvent(
            application_id=application.id,
            type="tailoring_selected",
            payload_json={"resume_id": str(body.resume_id)},
        )
    )
    await session.commit()
    await session.refresh(application)
    return application


@router.post("/{application_id}/otp", response_model=ApplicationOut)
async def submit_otp(
    application_id: uuid.UUID, body: OtpSubmission, session: SessionDep
) -> Application:
    """Supply a verification code to an application parked at `needs_otp`.

    Without this the state machine has a dead end: a run that hits an OTP
    challenge parks and can never resume.

    The code is stored on the application for the worker to consume and is
    deliberately kept out of the event payload, which is append-only.
    """
    application = await session.get(Application, application_id)
    if application is None:
        raise ApiError(ErrorCode.NOT_FOUND, "application not found")

    if application.status != ApplicationStatus.NEEDS_OTP.value:
        raise ApiError(
            ErrorCode.INVALID_STATE,
            f"application is {application.status}, not needs_otp",
        )

    application.review_json = {**(application.review_json or {}), "otp": body.code}

    await transition(
        session,
        application,
        ApplicationStatus.RUNNING,
        payload={"otp_supplied": True},
    )
    await enqueue(session, APPLY_TASK_KIND, {"application_id": str(application.id)})

    await session.commit()
    await session.refresh(application)
    return application


#: How much of the job description the handoff screen carries. The full text
#: stays on the posting record; this is what a person reads before deciding
#: they recognize the job.
DESCRIPTION_PREVIEW_CHARS = 4000


@router.get("/{application_id}/packet", response_model=ApplicationPacketOut)
async def application_packet(
    application_id: uuid.UUID, session: SessionDep
) -> ApplicationPacketOut:
    """Everything needed to finish this application by hand.

    Every ATS this project supports mounts a captcha at the fill stage, and
    §2.5 rules out working around one, so submission is the owner's step. The
    run still did the expensive parts — it found the posting, scored it,
    tailored the résumé, and answered the form — and this is where that work
    is handed over: the posting to confirm, the file to upload, the answers to
    copy in the employer's own wording, and the questions nobody could answer.
    """
    application = await session.get(Application, application_id)
    if application is None:
        raise ApiError(ErrorCode.NOT_FOUND, "application not found")

    review = application.review_json or {}

    posting_out: PacketPosting | None = None
    if application.posting_id is not None:
        posting = await session.get(Posting, application.posting_id)
        if posting is not None:
            company = (
                await session.get(Company, posting.company_id)
                if posting.company_id is not None
                else None
            )
            description = posting.description_raw or ""
            posting_out = PacketPosting(
                title=posting.title,
                company=company.name if company else None,
                location=posting.location,
                url=posting.url,
                description=description[:DESCRIPTION_PREVIEW_CHARS] or None,
            )

    resume_out = await _packet_resume(session, application, review)

    # `value` is None for file uploads and anything the adapter redacted, and
    # a blank row on the handoff screen is worse than no row.
    answers = [
        PacketAnswer(question=field["label"], value=str(field["value"]))
        for field in review.get("filled") or []
        if field.get("label") and field.get("value") is not None
    ]

    unanswered = [
        PacketQuestion(
            question=item["question"],
            kind=item.get("kind"),
            required=bool(item.get("required")),
        )
        for item in review.get("unanswered") or []
        if item.get("question")
    ]

    screenshot_ref = review.get("screenshot_ref")
    screenshot_path = None
    if screenshot_ref:
        path = get_storage().path_for(screenshot_ref)
        screenshot_path = str(path) if path.is_file() else None

    return ApplicationPacketOut(
        application_id=application.id,
        status=application.status,
        failure_reason=application.failure_reason,
        ats=application.ats,
        apply_url=application.url,
        posting=posting_out,
        resume=resume_out,
        answers=answers,
        unanswered=unanswered,
        screenshot_path=screenshot_path,
        ready_to_submit=bool(screenshot_ref)
        and not any(question.required for question in unanswered),
    )


async def _packet_resume(
    session: AsyncSession, application: Application, review: dict[str, object]
) -> PacketResume | None:
    """The document to upload — tailored if one was rendered, base otherwise.

    Falling back to the base résumé matters more than it looks. Tailoring is
    an enhancement that is allowed to fail, and when it does the owner still
    needs a file; handing back nothing would turn a degraded run into a
    useless one. `is_tailored` says which they got.
    """
    diff = review.get("resume_diff") or {}
    resume_id = application.tailored_resume_id
    is_tailored = resume_id is not None

    if resume_id is None:
        profile = await session.get(Profile, application.profile_id)
        if profile is None or profile.base_resume_id is None:
            return None
        resume_id = profile.base_resume_id

    resume = await session.get(Resume, resume_id)
    if resume is None:
        return None

    return PacketResume(
        resume_id=resume.id,
        download_path=f"/resumes/{resume.id}/file",
        is_tailored=is_tailored,
        rewritten_bullets=int(diff.get("changed", 0)) if isinstance(diff, dict) else 0,
        rejected_rewrites=int(diff.get("rejected", 0)) if isinstance(diff, dict) else 0,
    )


@router.post("/{application_id}/submitted", response_model=ApplicationOut)
async def mark_submitted(
    application_id: uuid.UUID, body: ManualSubmission, session: SessionDep
) -> Application:
    """Record that the owner submitted this application by hand.

    The end of the path §2.5 forces. Every supported ATS mounts a captcha on
    the apply form, this project will not work around one, so the last click
    is the owner's — and until now there was nowhere to put the fact that they
    made it. A finished application sat on the board as `needs_review` or
    `failed[manual_completion_required]` forever, which makes the pipeline
    lie and makes the funnel report meaningless.

    This is the only route that may move a terminal row, and only to
    `submitted`. The worker cannot reach it, so redelivery still cannot
    resurrect anything — a person saying "I sent this myself" is a different
    thing from a retry.
    """
    application = await session.get(Application, application_id)
    if application is None:
        raise ApiError(ErrorCode.NOT_FOUND, "application not found")

    if application.status == ApplicationStatus.SUBMITTED.value:
        # Already recorded. Idempotent rather than an error: at a hundred a
        # day the owner will double-tap, and a queue that punishes that is a
        # queue that gets used slowly.
        return application

    await transition(
        session,
        application,
        ApplicationStatus.SUBMITTED,
        payload={
            "by": "owner",
            "note": body.note,
            # Kept because it says *why* this needed a person: a captcha, an
            # unsupported site, a question nobody could answer.
            "was": application.status,
            "failure_reason": application.failure_reason,
        },
    )
    # The reason it could not be automated is history now, not current state.
    application.failure_reason = None
    await session.commit()
    await session.refresh(application)
    return application


@router.get("/queue/manual", response_model=list[ApplicationPacketOut])
async def manual_queue(
    session: SessionDep,
    limit: int = Query(default=25, ge=1, le=100),
) -> list[ApplicationPacketOut]:
    """Everything waiting on the owner, as complete handoff packets.

    One request rather than one per application. At a hundred a day the round
    trips are the difference between a queue that flows and one that stutters,
    and every packet is needed anyway the moment its card is shown.
    """
    stmt = (
        select(Application)
        .where(
            Application.status.in_(
                [ApplicationStatus.NEEDS_REVIEW.value, ApplicationStatus.FAILED.value]
            )
        )
        .order_by(Application.updated_at.asc())
        .limit(limit)
    )
    waiting = (await session.scalars(stmt)).all()

    packets: list[ApplicationPacketOut] = []
    for application in waiting:
        # A failed application only belongs here if a person could finish it.
        if (
            application.status == ApplicationStatus.FAILED.value
            and application.failure_reason != FailureReason.MANUAL_COMPLETION_REQUIRED.value
        ):
            continue
        packets.append(await application_packet(application.id, session))
    return packets
