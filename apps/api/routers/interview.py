"""Application-scoped interview preparation."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from packages.core.enums import ErrorCode
from packages.core.models import Application, Company, Posting, QueueTask, Resume
from packages.core.models_interview import InterviewJudgment, InterviewPlan, InterviewRound
from packages.interview import service
from packages.interview.mock import MockInput, advance
from packages.interview.plan import suggest
from packages.interview.schema import Confirm, Details, Snapshot

router = APIRouter(prefix="/applications/{application_id}/interview", tags=["interview"])


class RevisionOut(BaseModel):
    id: UUID
    round_id: UUID
    status: str
    queue_status: str | None
    attempts: int | None
    created_at: datetime
    inputs: Snapshot
    progress: dict[str, Any]


class InvitationOut(BaseModel):
    id: UUID
    subject: str | None
    at: datetime
    inferred: bool
    round_id: UUID | None
    suggested: Details


class ResumeVersion(BaseModel):
    id: UUID
    version: int


class ResumeOption(ResumeVersion):
    created_at: datetime


class RoundOut(BaseModel):
    id: UUID
    task_id: UUID | None
    notes: str | None
    plans: list[RevisionOut]


class InterviewOut(BaseModel):
    company: str | None
    role: str | None
    fixture: bool
    submitted_resume: ResumeVersion | None
    resumes: list[ResumeOption]
    invitations: list[InvitationOut]
    rounds: list[RoundOut]


async def _application(session: SessionDep, application_id: UUID) -> Application:
    app = await session.get(Application, application_id)
    if app is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Application not found.")
    return app


def _round_query(application_id: UUID) -> Select[tuple[InterviewRound]]:
    return select(InterviewRound).where(InterviewRound.application_id == application_id)


async def _plan(session: SessionDep, application_id: UUID, plan_id: UUID) -> InterviewPlan:
    row = await session.scalar(
        select(InterviewPlan)
        .join(InterviewRound)
        .where(InterviewRound.application_id == application_id, InterviewPlan.id == plan_id)
        .with_for_update(of=InterviewPlan)
    )
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Prep plan not found.")
    return row


def _revision(row: InterviewPlan, queue: QueueTask | None = None) -> dict[str, Any]:
    status = row.status
    if queue is None and row.queue_id is None and status in {"queued", "running"}:
        status = "interrupted"
    if queue and queue.status in {"failed", "cancelled"} and status in {"queued", "running"}:
        status = queue.status
    return {
        "id": row.id,
        "round_id": row.round_id,
        "status": status,
        "queue_status": queue.status if queue else None,
        "attempts": queue.attempts if queue else None,
        "created_at": row.created_at,
        "inputs": row.inputs,
        "progress": row.progress,
    }


@router.get("", response_model=InterviewOut)
async def get_interview(application_id: UUID, session: SessionDep) -> dict[str, Any]:
    app = await _application(session, application_id)
    rounds = list(
        await session.scalars(
            _round_query(application_id).order_by(InterviewRound.created_at.desc())
        )
    )
    revisions = (
        await session.execute(
            select(InterviewPlan, QueueTask)
            .join(InterviewRound)
            .outerjoin(QueueTask, InterviewPlan.queue_id == QueueTask.id)
            .where(InterviewRound.application_id == app.id)
            .order_by(InterviewPlan.created_at.desc())
        )
    ).all()
    plans: dict[UUID, list[dict[str, Any]]] = {}
    used: dict[str, UUID] = {}
    for plan, queue in revisions:
        plans.setdefault(plan.round_id, []).append(_revision(plan, queue))
        if plan.source_key:
            used[plan.source_key] = plan.round_id
    seen_invites = set()
    invites = []
    for mail in await service.invitations(session, app):
        digest = service.source_key(mail)
        if digest in seen_invites:
            continue
        seen_invites.add(digest)
        invites.append(
            {
                "id": mail.id,
                "subject": mail.subject,
                "at": mail.at,
                "inferred": mail.link_method != "alias",
                "round_id": used.get(digest),
                "suggested": suggest(service.invitation_text(mail)),
            }
        )
    resumes = (
        await session.execute(
            select(Resume.id, Resume.version, Resume.created_at)
            .where(Resume.candidate_id == app.candidate_id)
            .order_by(Resume.version.desc(), Resume.created_at.desc())
        )
    ).all()
    posting = await session.get(Posting, app.posting_id) if app.posting_id else None
    company = (
        await session.get(Company, posting.company_id) if posting and posting.company_id else None
    )
    sent = service.submitted_document(app)
    return {
        "company": company.name if company else "",
        "role": posting.title if posting else "",
        "fixture": (app.receipt_json or {}).get("interview_fixture") is True,
        "submitted_resume": {"id": sent["id"], "version": sent["version"]} if sent else None,
        "resumes": [dict(row._mapping) for row in resumes],
        "invitations": invites,
        "rounds": [
            {
                "id": row.id,
                "task_id": row.task_id,
                "notes": row.notes,
                "plans": plans.get(row.id, []),
            }
            for row in rounds
        ],
    }


class RecruiterNote(BaseModel):
    note: Annotated[str, Field(max_length=2500)]


@router.post("/suggest")
async def suggest_note(application_id: UUID, body: RecruiterNote, session: SessionDep) -> Details:
    await _application(session, application_id)
    return suggest(body.note)


@router.post("", response_model=RevisionOut)
async def confirm_interview(
    application_id: UUID, body: Confirm, session: SessionDep
) -> dict[str, Any]:
    try:
        row = await service.confirm(session, application_id, body)
    except service.PrepInputError as exc:
        raise ApiError(ErrorCode.INVALID_REQUEST, str(exc)) from None
    await session.commit()
    return _revision(row)


@router.post("/plans/{plan_id}/retry", response_model=RevisionOut)
async def retry(application_id: UUID, plan_id: UUID, session: SessionDep) -> dict[str, Any]:
    plan = await _plan(session, application_id, plan_id)
    queue = await session.get(QueueTask, plan.queue_id) if plan.queue_id else None
    can_resume = queue is None or queue.status in {"done", "failed", "cancelled"}
    if can_resume and plan.status in {"interrupted", "queued", "running"}:
        await service.queue_plan(session, plan)
    await session.commit()
    return _revision(plan, await session.get(QueueTask, plan.queue_id) if plan.queue_id else None)


class RoundNotes(BaseModel):
    notes: Annotated[str, Field(max_length=6000)]


@router.put("/rounds/{round_id}/notes")
async def save_notes(
    application_id: UUID,
    round_id: UUID,
    body: RoundNotes,
    session: SessionDep,
) -> dict[str, Any]:
    row = await session.scalar(_round_query(application_id).where(InterviewRound.id == round_id))
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Round not found.")
    row.notes = body.notes.strip() or None
    await session.commit()
    return {"notes": row.notes}


class Judgment(BaseModel):
    requirement: Annotated[str, Field(max_length=64)]
    evidence: Annotated[str, Field(max_length=64)]
    grade: Annotated[int, Field(ge=0, le=2)]


@router.put("/plans/{plan_id}/judgment")
async def judge(
    application_id: UUID,
    plan_id: UUID,
    body: Judgment,
    session: SessionDep,
) -> Judgment:
    row = await _plan(session, application_id, plan_id)
    evidence = {
        item["id"]
        for item in row.progress["evidence"]
        if item["kind"] in {"resume", "project", "coursework", "internship"}
    }
    requirements = {item["requirement"]["id"] for item in row.progress["plan"]["coverage"]}
    if body.evidence not in evidence or body.requirement not in requirements:
        raise ApiError(ErrorCode.INVALID_REQUEST, "Choose a requirement and candidate excerpt.")
    await session.execute(
        insert(InterviewJudgment)
        .values(plan_id=row.id, **body.model_dump())
        .on_conflict_do_update(constraint="uq_interview_judgment", set_={"grade": body.grade})
    )
    await session.commit()
    return body


@router.post("/plans/{plan_id}/mock")
async def mock_turn(
    application_id: UUID,
    plan_id: UUID,
    body: MockInput,
    session: SessionDep,
) -> dict[str, Any]:
    row = await _plan(session, application_id, plan_id)
    try:
        mock = await advance(
            row.progress,
            body,
            fixture=bool(row.inputs.get("fixture")),
            share_mail=bool(row.inputs.get("share_mail")),
        )
    except ValueError as exc:
        raise ApiError(ErrorCode.INVALID_REQUEST, str(exc)) from None
    row.progress = {**row.progress, "mock": mock}
    await session.commit()
    return mock
