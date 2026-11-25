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
from apps.api.studio_projects import refresh_projects
from packages.core.enums import ErrorCode
from packages.core.models import Project, Resume
from packages.core.schemas import ResumeOut
from packages.core.schemas_studio import (
    StudioPreview,
    StudioRequest,
    StudioSave,
    StudioSaved,
)
from packages.core.storage import get_storage, resume_key
from packages.llm import router as llm_router
from packages.llm.provider import LLMError
from packages.matching.skill_links import link_resume
from packages.tailor.assemble import assemble_pdf
from packages.tailor.code_evidence import (
    add_linked_claim,
    claim_destination,
    retrieve_evidence,
)
from packages.tailor.evidence import linked_evidence
from packages.tailor.guard import line_key
from packages.tailor.kept_findings import FINDINGS_KEY, kept
from packages.tailor.parse import Contact, ParsedResume
from packages.tailor.project_rewrite import validate_description, with_description
from packages.tailor.project_selection import saved_declarations
from packages.tailor.publish import house_options
from packages.tailor.studio import (
    STUDIO_STAMP,
    RoleTrack,
    as_printed,
    project_lines,
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
from packages.tailor.studio_preview import assemble_live_code_preview, preview_of
from packages.tailor.verify import check_pdf

router = APIRouter(prefix="/resumes", tags=["resume studio"])

#: How many unbacked lines a refusal quotes. The rest are counted.
QUOTED_REFUSALS = 3


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
    projects = await candidate_projects(session, body.candidate_id)
    notice = "Imported GitHub metadata; live checking was not requested."
    if body.live_projects and not body.live_code:
        try:
            projects, notice = await refresh_projects(projects)
        except LLMError as exc:
            raise ApiError(ErrorCode.INVALID_REQUEST, str(exc)) from exc
    provider = None
    if body.use_model:
        provider = (
            llm_router.build_provider("kaggle")
            if body.model_provider == "kaggle"
            else llm_router.tailor_resume()
        )
    if body.live_code:
        code = await retrieve_evidence(body.job_description, projects)
        return await assemble_live_code_preview(
            base_row=base_row,
            base=base,
            source=source,
            job_description=body.job_description,
            track=track,
            projects=projects,
            code=code,
            selected_project_ids=body.selected_project_ids,
            project_limit=body.project_limit,
            provider=provider,
        )
    draft = await synthesize(
        base,
        source,
        body.job_description,
        track,
        projects=projects,
        linked=await linked_evidence(session, body.candidate_id),
        selected_project_ids=(
            None
            if body.selected_project_ids is None
            else [str(i) for i in body.selected_project_ids]
        ),
        project_limit=body.project_limit,
        provider=provider,
        replace_source_projects=body.replace_source_projects,
        declared=None if body.live_projects else await saved_declarations(session, projects),
        code_verified=None,
    )
    preview = preview_of(draft, base_row, track)
    preview.project_evidence = notice
    return preview


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
        if project.is_fork:
            raise ApiError(ErrorCode.INVALID_REQUEST, "Forked projects cannot be selected.")
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
    if body.live_projects and not body.live_code and projects:
        try:
            checked, _ = await refresh_projects(projects)
        except LLMError as exc:
            raise ApiError(ErrorCode.INVALID_REQUEST, str(exc)) from exc
        by_id = {project.id: project for project in checked}
        if set(by_id) != {project.id for project in projects}:
            raise ApiError(
                ErrorCode.INVALID_REQUEST,
                "A selected project could not be checked. Generate a new preview.",
            )
        projects = [by_id[project.id] for project in projects]
    if body.live_code and body.project_rewrites:
        raise ApiError(ErrorCode.INVALID_REQUEST, "Review live project claims individually.")
    if (body.code_approvals and not body.live_code) or (
        body.live_code and (body.code_approvals or body.rewrites) and not body.job_description
    ):
        raise ApiError(ErrorCode.INVALID_REQUEST, "Generate a live GitHub code evidence preview.")
    by_id = {project.id: project for project in projects}
    rewritten_ids = [item.project_id for item in body.project_rewrites]
    if len(set(rewritten_ids)) != len(rewritten_ids) or not set(rewritten_ids) <= set(by_id):
        raise ApiError(ErrorCode.INVALID_REQUEST, "Invalid project rewrite selection.")
    for item in body.project_rewrites:
        project = by_id[item.project_id]
        reason = validate_description(project, item.original, item.tailored)
        if reason:
            raise ApiError(ErrorCode.INVALID_REQUEST, reason)
        by_id[project.id] = with_description(project, item.tailored)
    projects = [by_id[project.id] for project in projects]
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
        linked=None if body.live_code else await linked_evidence(session, body.candidate_id),
        live_posting=(body.job_description or "") if body.live_code else None,
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

    claims = []
    verified_findings: list[dict[str, object]] = []
    approved_wording: list[str] = []
    approved_generated: dict[uuid.UUID, list[str]] = {}
    added_project_lines: list[str] = []
    if body.code_approvals and body.job_description:
        owned = await candidate_projects(session, body.candidate_id)
        code = await retrieve_evidence(body.job_description, owned)
        by_finding = {item.id: item for item in code.findings if item.can_support_claim}
        owned_by_id = {project.id: project for project in owned}
        proposed_wording = {
            line_key(wording) for item in by_finding.values() for wording in item.wording
        }
        for name, lines in sections.items():
            if name == "projects":
                continue
            existing = {line_key(line.lstrip("•-* ")) for line in source.resume.section(name)}
            if any(
                (key := line_key(line.lstrip("•-* "))) in proposed_wording and key not in existing
                for line in lines
            ):
                raise ApiError(ErrorCode.INVALID_REQUEST, "GitHub claims belong in Projects.")
        checked_ids: set[str] = set()
        for approval in body.code_approvals:
            finding = by_finding.get(approval.finding_id)
            if (
                finding is None
                or approval.finding_id in checked_ids
                or approval.wording not in finding.wording
            ):
                reason = " ".join(code.limitations)
                raise ApiError(
                    ErrorCode.INVALID_REQUEST,
                    "A project claim changed or could not be verified. "
                    f"Generate a new preview. {reason}".strip(),
                )
            checked_ids.add(approval.finding_id)
            project = owned_by_id[finding.project_id]
            destination = claim_destination(project, set(by_id), base)
            if destination is None or destination[1] != approval.entry:
                raise ApiError(ErrorCode.INVALID_REQUEST, "The approved Projects entry changed.")
            if approval.entry is None:
                approved_generated.setdefault(project.id, []).append(approval.wording)
            else:
                if not add_linked_claim(approval.entry, approval.wording, base, sections):
                    raise ApiError(ErrorCode.INVALID_REQUEST, "The linked Projects title changed.")
                added_project_lines.append(f"• {approval.wording}")
            approved_wording.append(approval.wording)
            claims.append(
                {
                    "id": finding.id,
                    "project_id": str(project.id),
                    "entry": approval.entry,
                    "commit_sha": finding.commit_sha,
                    "path": finding.path,
                    "start_line": finding.start_line,
                    "end_line": finding.end_line,
                    "wording": approval.wording,
                }
            )
        # Materialize approved project bullets as Projects entries in the saved
        # résumé itself so an adopted version can carry them into later tailoring.
        for project in projects:
            if project.id in approved_generated:
                title, *details = project_lines(project)
                lines = [
                    f"{title} [GitHub]",
                    *(f"• {wording}" for wording in approved_generated[project.id]),
                    *details,
                ]
                sections.setdefault("projects", []).extend(lines)
                added_project_lines.extend(lines)
        projects = [project for project in projects if project.id not in approved_generated]
        # This check's public findings outlive the preview: skill-gap answers
        # cite them as implementation (`kept_findings.py`).
        verified_findings = kept(code.findings)

    printed = {line_key(line) for lines in sections.values() for line in lines}
    final = ParsedResume(
        contact=Contact.model_validate(body.contact.model_dump()),
        preamble=list(source.resume.preamble),
        sections=sections,
        raw_lines=[
            *source.resume.raw_lines,
            *added_project_lines,
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

    if body.live_code:
        try:
            verdict = await asyncio.to_thread(
                check_pdf,
                pdf,
                as_printed(final, projects),
                terms=approved_wording,
            )
        except Exception as exc:
            raise ApiError(ErrorCode.INTERNAL_ERROR, "The draft PDF could not be checked.") from exc
        if not verdict.ok:
            raise ApiError(
                ErrorCode.INVALID_REQUEST, "The PDF did not preserve the reviewed draft."
            )

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
        "project_snapshots": {
            str(p.id): {
                key: getattr(p, key)
                for key in (
                    "name",
                    "description",
                    "url",
                    "language",
                    "topics_json",
                    "is_private",
                )
            }
            for p in projects
        },
    }
    if claims:
        stored[STUDIO_STAMP]["github_code_approvals"] = claims
    if verified_findings:
        stored[STUDIO_STAMP][FINDINGS_KEY] = verified_findings
    resume = Resume(
        candidate_id=body.candidate_id,
        version=version,
        storage_ref=key,
        parsed_json=stored,
        is_default=False,
        tailored_by=body.tailored_by if body.rewrites or body.project_rewrites else None,
    )
    session.add(resume)
    await session.flush()
    await link_resume(session, resume)
    if body.adopt and profile is not None:
        profile.base_resume_id = resume.id
    await session.commit()
    await session.refresh(resume)
    return StudioSaved(resume=ResumeOut.model_validate(resume), adopted=body.adopt)
