"""Create a round or a new revision on the application's existing tracker task."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config import get_settings
from packages.core.models import Application, Company, InboundMessage, Posting, Project, Resume
from packages.core.models_interview import InterviewPlan, InterviewRound
from packages.core.models_jobs import PostingVersion
from packages.core.models_skill_links import ProjectSkill
from packages.core.models_tracking import ApplicationTask
from packages.core.queue import enqueue
from packages.github.client import GitHubError
from packages.github.live import eligible, read_live_project
from packages.interview.plan import build_plan, evidence, invitation_key, key, requirements
from packages.interview.schema import Confirm, Evidence, Snapshot, SourceKind
from packages.matching.graph_rag import project_keys
from packages.tailor.parse import ParsedResume
from packages.tailor.studio_load import candidate_projects, confirmed_evidence
from packages.tracking.tasks import checklist

TASK_KIND = "interview_prep"
INVITE_KINDS = ("interview", "assessment")


class PrepInputError(ValueError):
    pass


def invitation_text(message: InboundMessage) -> str:
    return f"{message.subject or ''}\n{message.body or ''}".strip()


def source_key(message: InboundMessage) -> str:
    assert message.application_id is not None
    return invitation_key(message.application_id, message.subject or "", message.body or "")


async def invitations(session: AsyncSession, app: Application) -> list[InboundMessage]:
    rows = await session.scalars(
        select(InboundMessage)
        .where(
            InboundMessage.application_id == app.id,
            InboundMessage.candidate_id == app.candidate_id,
            InboundMessage.classification.in_(INVITE_KINDS),
        )
        .order_by(InboundMessage.at.desc(), InboundMessage.id)
    )
    return list(rows)


def submitted_document(app: Application) -> dict[str, Any]:
    # Only the successful submit path records this. A stale tailored selection is
    # not evidence of submission, even for applications with submitted status.
    return (app.submission_sources or {}).get("resume") or {}


async def _resume(
    session: AsyncSession, app: Application, body: Confirm
) -> tuple[str, int | None, dict[str, Any] | None]:
    sent = submitted_document(app)
    if sent:
        if body.resume_unknown or (body.resume_id and str(body.resume_id) != sent["id"]):
            raise PrepInputError("The application already records which résumé was submitted.")
        return f"Résumé {sent['id']}", sent["version"], sent.get("parsed")
    if body.resume_unknown and body.resume_id is None:
        return "Submitted résumé unknown", None, None
    if body.resume_id is None or body.resume_unknown:
        raise PrepInputError("Choose the submitted résumé version or mark it unknown.")
    resume = await session.get(Resume, body.resume_id)
    if not resume or resume.candidate_id != app.candidate_id:
        raise PrepInputError("Résumé version does not belong to this application’s candidate.")
    return f"Owner-selected résumé {resume.id}", resume.version, resume.parsed_json


async def _snapshot(
    session: AsyncSession, app: Application, body: Confirm, message: InboundMessage | None
) -> tuple[Snapshot, dict[str, Any] | None]:
    posting = await session.get(Posting, app.posting_id) if app.posting_id else None
    company = (
        await session.get(Company, posting.company_id) if posting and posting.company_id else None
    )
    sent_posting = (app.submission_sources or {}).get("posting") or {}
    description = (
        sent_posting.get("description") or ""
        if sent_posting
        else ((posting.description_raw or "") if posting else "")
    )
    if body.description is not None:
        description = body.description
    version = sent_posting.get("version") if sent_posting else None
    if posting and not sent_posting and body.description is None:
        version = await session.scalar(
            select(PostingVersion.version)
            .where(
                PostingVersion.posting_id == posting.id,
                PostingVersion.content_hash == posting.content_hash,
            )
            .order_by(PostingVersion.version.desc())
            .limit(1)
        )
    ref = (
        "Owner-supplied job description"
        if body.description is not None
        else (
            ("Submission posting snapshot " if sent_posting else "Current posting snapshot ")
            + str(sent_posting.get("id") or (posting.id if posting else app.url))
        )
    )
    resume_ref, resume_version, parsed = await _resume(session, app, body)
    return Snapshot(
        company=sent_posting.get("company") or (company.name if company else ""),
        company_domain=sent_posting.get("company_domain")
        or (company.domain or "" if company else ""),
        role=sent_posting.get("role") or (posting.title or "" if posting else ""),
        location=sent_posting.get("location") or (posting.location or "" if posting else ""),
        details=body.details,
        posting_ref=ref,
        posting_description=description,
        posting_hash=key(description),
        posting_version=version,
        resume_ref=resume_ref,
        resume_id=UUID(submitted_document(app)["id"])
        if submitted_document(app)
        else body.resume_id,
        resume_version=resume_version,
        invitation_id=message.id if message else None,
        recruiter_excerpt=(invitation_text(message) if message else body.recruiter_note)[:6000],
        prep_minutes=body.prep_minutes,
        urls=[str(url) for url in body.research_urls],
        research=body.research,
        share_mail=body.share_mail,
        fixture=(app.receipt_json or {}).get("interview_fixture") is True,
    ), parsed


async def collect_local(
    session: AsyncSession,
    app: Application,
    snapshot: Snapshot,
    parsed: dict[str, Any] | None,
    round_id: UUID,
) -> list[Evidence]:
    sources = [
        evidence("posting", snapshot.posting_ref, req.wording, url=app.url)
        for req in requirements(snapshot.posting_description)
    ]
    confirmed = snapshot.details.model_dump_json(exclude_none=True, exclude_defaults=True)
    if confirmed != "{}":
        reference = (
            f"Confirmed from inbox message {snapshot.invitation_id}"
            if snapshot.invitation_id
            else "Owner-confirmed round details"
        )
        sources.append(evidence("recruiter-confirmed", reference, confirmed))
    if parsed:
        resume = ParsedResume.model_validate(parsed)
        for section in (
            "experience",
            "projects",
            "education",
            "coursework",
            "skills",
            "certifications",
        ):
            for line in resume.section(section):
                kind: SourceKind = (
                    "coursework" if section in {"education", "coursework"} else "resume"
                )
                # A résumé project remains project evidence even when submitted.
                if section == "projects":
                    kind = "project"
                sources.append(evidence(kind, f"{snapshot.resume_ref} / {section}", line))
    internships, _ = await confirmed_evidence(session, app.candidate_id)
    for job in internships:
        for bullet in job.bullets:
            sources.append(
                evidence("internship", f"{job.role} at {job.company} ({job.id})", bullet)
            )
    projects = [
        project
        for project in await candidate_projects(session, app.candidate_id)
        if eligible(project)
    ]
    links = list(
        await session.scalars(
            select(ProjectSkill).where(ProjectSkill.project_id.in_([p.id for p in projects]))
        )
    )
    for project in projects:
        stack = [project.language or ""]
        current = project_keys(project)
        if project.dependencies_pushed_at == project.pushed_at:
            stack.extend(project.dependency_technologies or [])
            stack.extend(
                link.declared_as
                for link in links
                if link.project_id == project.id and link.skill in current
            )
        stack = list(dict.fromkeys(stack))
        for item in [project.description or "", *stack]:
            if item:
                sources.append(
                    evidence(
                        "project",
                        f"GitHub inventory: {project.full_name}",
                        item,
                        project_id=project.id,
                        url=f"https://github.com/{project.full_name}",
                        retrieved=project.synced_at.isoformat(),
                        caveat="Project evidence establishes personal-project scope.",
                    )
                )
    for earlier in await session.scalars(
        select(InterviewRound)
        .where(
            InterviewRound.application_id == app.id,
            InterviewRound.id != round_id,
            InterviewRound.notes.is_not(None),
        )
        .order_by(InterviewRound.created_at.desc())
        .limit(4)
    ):
        if earlier.notes:
            sources.append(evidence("round notes", f"Round {earlier.id}", earlier.notes))
    return list({s.id: s for s in sources if s.excerpt.strip()}.values())


async def confirm(
    session: AsyncSession,
    application_id: UUID,
    body: Confirm,
) -> InterviewPlan:
    app = await session.get(Application, application_id, with_for_update=True)
    if app is None:
        raise PrepInputError("Application not found.")
    rounds = list(
        await session.scalars(select(InterviewRound).where(InterviewRound.application_id == app.id))
    )
    round_ids = {row.id for row in rounds}
    existing = await session.scalar(
        select(InterviewPlan).where(InterviewPlan.request_id == body.request_id)
    )
    if existing:
        if existing.round_id not in round_ids:
            raise PrepInputError("Request ID belongs to another application.")
        return existing
    message = None
    digest = None
    if body.invitation_id:
        message = await session.get(InboundMessage, body.invitation_id)
        if (
            message is None
            or message.application_id != app.id
            or message.candidate_id != app.candidate_id
            or message.classification not in INVITE_KINDS
        ):
            raise PrepInputError("Invitation does not belong to this application.")
        digest = source_key(message)
    elif body.recruiter_note.strip():
        digest = invitation_key(app.id, "", body.recruiter_note)
    selected = next((row for row in rounds if row.id == body.round_id), None)
    if body.round_id and selected is None:
        raise PrepInputError("Round does not belong to this application.")
    if digest and round_ids:
        duplicate = await session.scalar(
            select(InterviewPlan)
            .where(InterviewPlan.round_id.in_(round_ids), InterviewPlan.source_key == digest)
            .order_by(InterviewPlan.created_at.desc())
            .limit(1)
        )
        if duplicate and selected is None:
            return duplicate
        if duplicate and selected and duplicate.round_id != selected.id:
            raise PrepInputError("That invitation is already attached to a different round.")
    snapshot, parsed = await _snapshot(session, app, body, message)
    if selected and body.description is None:
        prior = await session.scalar(
            select(InterviewPlan)
            .where(InterviewPlan.round_id == selected.id)
            .order_by(InterviewPlan.created_at.desc(), InterviewPlan.id)
            .limit(1)
        )
        if prior:
            # A schedule change must not silently replace the posting the revision was based on.
            saved = Snapshot.model_validate(prior.inputs)
            for field in (
                "posting_ref",
                "posting_description",
                "posting_hash",
                "posting_version",
                "company",
                "company_domain",
                "role",
                "location",
            ):
                setattr(snapshot, field, getattr(saved, field))
    if selected is None:
        kind = message.classification if message else "interview"
        assert kind in INVITE_KINDS
        task = await session.scalar(
            select(ApplicationTask)
            .where(
                ApplicationTask.application_id == app.id,
                ApplicationTask.kind == kind,
                ApplicationTask.completed_at.is_(None),
                ApplicationTask.id.not_in([row.task_id for row in rounds if row.task_id]),
            )
            .order_by(ApplicationTask.created_at.desc())
            .limit(1)
        )
        if task is None:
            task = ApplicationTask(
                application_id=app.id,
                kind=kind,
                title=body.details.round or "Interview prep",
                source="inbox" if message and message.link_method == "alias" else "owner",
                checklist_json=checklist(kind),
            )
            session.add(task)
            await session.flush()
        selected = InterviewRound(application_id=app.id, task_id=task.id)
        session.add(selected)
        await session.flush()
    task = await session.get(ApplicationTask, selected.task_id) if selected.task_id else None
    details = snapshot.details
    if task and details.date and details.time and details.timezone:
        try:
            tz = ZoneInfo(details.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            pass
        else:
            due = datetime.combine(details.date, details.time, tzinfo=tz)
            task.due_at = due
            if task.reminder_at and task.reminder_at > due:
                task.reminder_at = None
            task.updated_at = datetime.now(UTC)
    sources = await collect_local(session, app, snapshot, parsed, selected.id)
    baseline = build_plan(snapshot, sources)
    row = InterviewPlan(
        round_id=selected.id,
        request_id=body.request_id,
        source_key=digest,
        inputs=snapshot.model_dump(mode="json"),
        progress={
            "evidence": [item.model_dump(mode="json") for item in sources],
            "plan": baseline.model_dump(mode="json"),
            "steps": [{"action": "collect_candidate_and_posting", "count": len(sources)}],
        },
        status=(
            "queued"
            if snapshot.research
            or snapshot.urls
            or snapshot.fixture
            or get_settings().interview_qwen_embeddings
            else "local"
        ),
    )
    session.add(row)
    await session.flush()
    if row.status == "queued":
        await queue_plan(session, row)
    return row


async def queue_plan(session: AsyncSession, plan: InterviewPlan) -> None:
    queued = await enqueue(session, TASK_KIND, {"plan_id": str(plan.id)})
    plan.queue_id = queued.id
    plan.status = "queued"


def now() -> str:
    return datetime.now(UTC).isoformat()


async def refresh_project(
    session: AsyncSession, application_id: UUID, project_id: UUID
) -> list[Evidence]:
    application = await session.get(Application, application_id)
    project = await session.get(Project, project_id)
    if application is None or project is None or project.candidate_id != application.candidate_id:
        return []
    try:
        async with asyncio.timeout(50):
            result = await read_live_project(session, project_id)
    except (GitHubError, TimeoutError):
        return []
    attribution = f"{result.full_name} · commit {result.commit_sha}"
    source_url = f"{result.url}/tree/{result.commit_sha}"
    items = [result.description, result.language, *result.technologies]
    return [
        evidence(
            "project",
            attribution,
            item,
            project_id=project_id,
            url=source_url,
            retrieved=result.checked_at.isoformat(),
            caveat="Verified public repository. Evidence is limited to the project.",
        )
        for item in dict.fromkeys(items)
        if item
    ]
