"""Resume Studio routes: preview a role-targeted draft, then keep it as a version.

`packages/tailor/studio.py` says what a draft may contain. Two routes, so a
preview never writes anything: `POST /resumes/synthesize` returns the draft,
the evidence behind each bullet and the ATS reading before and after, and
`POST /resumes/synthesize/save` keeps what the owner approved, after judging
every line again (`studio.saved_lines_check`).
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from packages.core.enums import ErrorCode
from packages.core.models import Project, Resume
from packages.core.schemas import ResumeContactEdit, ResumeOut
from packages.core.schemas_studio import (
    StudioAts,
    StudioBullet,
    StudioLine,
    StudioPreview,
    StudioProject,
    StudioRequest,
    StudioSave,
    StudioSaved,
    StudioTarget,
    StudioTerm,
)
from packages.core.storage import get_storage, resume_key
from packages.llm import router as llm_router
from packages.tailor.assemble import assemble_pdf
from packages.tailor.ats import AtsReport
from packages.tailor.evidence import linked_evidence
from packages.tailor.guard import line_key
from packages.tailor.parse import Contact, ParsedResume
from packages.tailor.publish import house_options
from packages.tailor.studio import (
    ROLE_PRESETS,
    STUDIO_STAMP,
    Draft,
    RoleTrack,
    saved_lines_check,
    synthesize,
)
from packages.tailor.studio_load import (
    StudioInputError,
    candidate_projects,
    resolve_base,
    resolve_profile,
    studio_source,
)

router = APIRouter(prefix="/resumes", tags=["resume studio"])

#: How many unbacked lines a refusal quotes. The rest are counted.
QUOTED_REFUSALS = 3


def _ats(report: AtsReport) -> StudioAts:
    return StudioAts(
        parse=report.parse,
        keywords=report.keywords,
        overall=report.overall,
        supported=list(report.supported),
        missing=list(report.missing),
        findings=[str(finding) for finding in report.findings],
    )


def _lines(draft: Draft) -> dict[str, list[StudioLine]]:
    """Every draft line with where it came from. A rewrite names the line it replaced."""
    rewritten = {line_key(b.tailored): b.original for b in draft.bullets if b.status == "rewritten"}
    out: dict[str, list[StudioLine]] = {}
    for section, lines in draft.resume.sections.items():
        rows = []
        for text in lines:
            original = rewritten.get(line_key(text))
            if original is not None:
                rows.append(StudioLine(text=text, origin="rewrite", original=original))
                continue
            origin = draft.source.origin_of(text)
            rows.append(StudioLine(text=text, origin=origin.kind, label=origin.label))
        out[section] = rows
    return out


def preview_of(draft: Draft, base: Resume, track: RoleTrack) -> StudioPreview:
    return StudioPreview(
        role_track=track.value,
        role_label=ROLE_PRESETS[track].label,
        base_resume_id=base.id,
        base_version=base.version,
        contact=ResumeContactEdit.model_validate(draft.resume.contact.model_dump()),
        sections={name: list(lines) for name, lines in draft.resume.sections.items()},
        lines=_lines(draft),
        html=draft.html,
        bullets=[
            StudioBullet(
                ref=b.ref,
                original=b.original,
                tailored=b.tailored,
                status=b.status,
                reason=b.reason,
                targets=[StudioTarget(term=t, evidence=e, kind=k) for t, e, k in b.targets],
            )
            for b in draft.bullets
        ],
        terms=[
            StudioTerm(term=t.term, status=t.status.value, sources=list(t.sources))
            for t in draft.plan.terms
        ],
        projects=[
            StudioProject(
                id=pick.project.id,
                name=pick.project.name,
                is_private=pick.project.is_private,
                score=pick.score,
                matched_terms=list(pick.matched_terms),
                chosen=pick.chosen,
                held_back=pick.held_back,
            )
            for pick in draft.picks
        ],
        ats_before=_ats(draft.ats_before),
        ats_after=_ats(draft.ats_after),
        added=list(draft.source.added),
        skipped=list(draft.source.skipped),
        tailored_by=draft.tailored_by,
        calls=draft.calls,
    )


@router.post("/synthesize", response_model=StudioPreview)
async def synthesize_draft(body: StudioRequest, session: SessionDep) -> StudioPreview:
    """A draft for one pasted posting. Writes nothing.

    With `use_model` the bullets that can gain a term go to the provider set
    for tailoring, which the audit trail records (CLAUDE.md §2.8).
    """
    try:
        profile = await resolve_profile(session, body.candidate_id, body.profile_id)
        base_row = await resolve_base(session, body.candidate_id, body.base_resume_id, profile)
    except StudioInputError as exc:
        raise ApiError(ErrorCode.INVALID_REQUEST, str(exc)) from exc
    base = ParsedResume.model_validate(base_row.parsed_json)
    source = await studio_source(
        session,
        body.candidate_id,
        base,
        include_internships=body.include_internships,
        certification_ids=body.selected_certification_ids,
    )
    track = RoleTrack(body.role_track)
    draft = await synthesize(
        base,
        source,
        body.job_description,
        track,
        projects=await candidate_projects(session, body.candidate_id),
        linked=await linked_evidence(session, body.candidate_id),
        selected_project_ids=(
            None
            if body.selected_project_ids is None
            else [str(i) for i in body.selected_project_ids]
        ),
        project_limit=body.project_limit,
        provider=llm_router.tailor_resume() if body.use_model else None,
    )
    return preview_of(draft, base_row, track)


async def _printable(
    session: AsyncSession, candidate_id: uuid.UUID, ids: list[uuid.UUID]
) -> list[Project]:
    """The projects to print, in the owner's order. A private one must be included or pinned."""
    owned = {p.id: p for p in await candidate_projects(session, candidate_id)}
    chosen = []
    for project_id in ids:
        project = owned.get(project_id)
        if project is None:
            raise ApiError(ErrorCode.INVALID_REQUEST, "a selected project is not this candidate's")
        if not project.resume_listed or project.include is False:
            raise ApiError(
                ErrorCode.INVALID_REQUEST,
                f"{project.name} is evidence only: include or pin it on /resumes to print it",
            )
        chosen.append(project)
    return chosen


async def _next_version(session: AsyncSession, candidate_id: uuid.UUID) -> int:
    highest = await session.scalar(
        select(func.max(Resume.version)).where(Resume.candidate_id == candidate_id)
    )
    return int(highest or 0) + 1


@router.post("/synthesize/save", response_model=StudioSaved, status_code=status.HTTP_201_CREATED)
async def save_draft(body: StudioSave, session: SessionDep) -> StudioSaved:
    """Keep an approved draft as a new version, rendered to PDF, and adopt it if asked.

    Every line is judged again: the source's own, a rewrite that still passes
    the guard, or one the owner typed here. Anything else is refused and
    nothing is saved.
    """
    try:
        profile = await resolve_profile(session, body.candidate_id, body.profile_id)
        base_row = await resolve_base(session, body.candidate_id, body.base_resume_id, None)
    except StudioInputError as exc:
        raise ApiError(ErrorCode.INVALID_REQUEST, str(exc)) from exc
    if body.adopt and profile is None:
        raise ApiError(ErrorCode.INVALID_REQUEST, "adopt needs the profile_id to adopt it into")
    base = ParsedResume.model_validate(base_row.parsed_json)
    source = await studio_source(
        session,
        body.candidate_id,
        base,
        include_internships=body.include_internships,
        certification_ids=body.selected_certification_ids,
    )
    projects = await _printable(session, body.candidate_id, body.selected_project_ids)
    sections = {
        name: [line.strip() for line in lines if line.strip()]
        for name, lines in body.sections.items()
        if any(line.strip() for line in lines)
    }
    refused = saved_lines_check(
        source,
        sections,
        rewrites=[(r.original, r.tailored) for r in body.rewrites],
        owner_edits=body.owner_edits,
        linked=await linked_evidence(session, body.candidate_id),
    )
    if refused:
        quoted = "; ".join(f"“{line[:120]}”" for line in refused[:QUOTED_REFUSALS])
        more = (
            f" and {len(refused) - QUOTED_REFUSALS} more" if len(refused) > QUOTED_REFUSALS else ""
        )
        raise ApiError(
            ErrorCode.INVALID_REQUEST,
            f"{len(refused)} line(s) are not in your sources: {quoted}{more}. "
            "Type them yourself on the studio screen, or remove them.",
        )

    printed = {line_key(line) for lines in sections.values() for line in lines}
    final = ParsedResume(
        contact=Contact.model_validate(body.contact.model_dump()),
        preamble=list(source.resume.preamble),
        sections=sections,
        raw_lines=[
            *source.resume.raw_lines,
            *(edit for edit in body.owner_edits if line_key(edit) in printed),
        ],
    )
    options = house_options().model_copy(
        update={"include_projects": bool(projects), "replace_source_projects": False}
    )
    try:
        pdf = await asyncio.to_thread(assemble_pdf, final, projects, options)
    except Exception as exc:  # noqa: BLE001 - WeasyPrint needs system libraries
        raise ApiError(
            ErrorCode.INTERNAL_ERROR,
            f"the draft could not be rendered to PDF ({type(exc).__name__}), so it was not saved",
        ) from exc

    version = await _next_version(session, body.candidate_id)
    key = resume_key(str(body.candidate_id), version, "studio.pdf")
    try:
        get_storage().put(key, pdf)
    except Exception as exc:  # noqa: BLE001 - surfaces size limits too
        raise ApiError(ErrorCode.INTERNAL_ERROR, f"could not store the draft: {exc}") from exc

    stored = final.model_dump(mode="json")
    stored[STUDIO_STAMP] = {
        "role_track": body.role_track,
        "project_ids": [str(p.id) for p in projects],
    }
    resume = Resume(
        candidate_id=body.candidate_id,
        version=version,
        storage_ref=key,
        parsed_json=stored,
        is_default=False,
        tailored_by=body.tailored_by if body.rewrites else None,
    )
    session.add(resume)
    await session.flush()
    if body.adopt and profile is not None:
        profile.base_resume_id = resume.id
    await session.commit()
    await session.refresh(resume)
    return StudioSaved(resume=ResumeOut.model_validate(resume), adopted=body.adopt)
