"""Résumé routes — upload, list, fetch, and set as a profile's base.

Résumés are PII (CLAUDE.md §2.8). The file itself goes to local storage; the
database holds a reference and the parsed structure, never the bytes.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile, status
from sqlalchemy import func, select

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from packages.core.config import get_settings
from packages.core.enums import ErrorCode
from packages.core.models import Candidate, Profile, Project, Resume
from packages.core.schemas import ResumeEdit, ResumeOut, ResumeParsedOut, ResumePreviewOut
from packages.core.storage import get_storage, resume_key
from packages.github.select import select_projects
from packages.tailor.assemble import describe
from packages.tailor.parse import ParsedResume, ParseError, parse_resume
from packages.tailor.projects import LinkStyle, ProjectEntry, link_text

router = APIRouter(prefix="/resumes", tags=["resumes"])

ALLOWED_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}


@router.post("", response_model=ResumeOut, status_code=status.HTTP_201_CREATED)
async def upload_resume(
    session: SessionDep,
    candidate_id: Annotated[uuid.UUID, Form()],
    file: Annotated[UploadFile, File()],
    is_default: Annotated[bool, Form()] = False,
) -> Resume:
    """Upload a résumé, parse it, and store it.

    Parsing happens at upload so a file that cannot be read fails here — while
    you are watching — rather than mid-application later.
    """
    candidate = await session.get(Candidate, candidate_id)
    if candidate is None:
        raise ApiError(ErrorCode.INVALID_REQUEST, "candidate_id does not exist")

    filename = file.filename or "resume"
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ApiError(
            ErrorCode.INVALID_REQUEST,
            f"unsupported format {suffix or 'unknown'}; use PDF, DOCX, or TXT",
        )

    data = await file.read()
    if not data:
        raise ApiError(ErrorCode.INVALID_REQUEST, "the uploaded file is empty")

    try:
        parsed = parse_resume(data, filename)
    except ParseError as exc:
        raise ApiError(ErrorCode.INVALID_REQUEST, str(exc)) from exc

    next_version = (
        await session.scalar(
            select(func.coalesce(func.max(Resume.version), 0) + 1).where(
                Resume.candidate_id == candidate_id
            )
        )
    ) or 1

    storage = get_storage()
    key = resume_key(str(candidate_id), next_version, Path(filename).name)
    try:
        storage.put(key, data)
    except Exception as exc:  # noqa: BLE001 - surfaces size limits too
        raise ApiError(ErrorCode.INVALID_REQUEST, f"could not store résumé: {exc}") from exc

    if is_default:
        for existing in (
            await session.scalars(select(Resume).where(Resume.candidate_id == candidate_id))
        ).all():
            existing.is_default = False

    resume = Resume(
        candidate_id=candidate_id,
        version=next_version,
        storage_ref=key,
        parsed_json=parsed.model_dump(),
        is_default=is_default,
    )
    session.add(resume)
    await session.commit()
    await session.refresh(resume)
    return resume


@router.get("", response_model=list[ResumeOut])
async def list_resumes(session: SessionDep, candidate_id: uuid.UUID) -> list[Resume]:
    result = await session.scalars(
        select(Resume).where(Resume.candidate_id == candidate_id).order_by(Resume.version.desc())
    )
    return list(result.all())


@router.get("/{resume_id}/parsed", response_model=ResumeParsedOut)
async def get_parsed(resume_id: uuid.UUID, session: SessionDep) -> ResumeParsedOut:
    """The extracted structure, so you can check what the parser actually saw.

    Worth looking at before trusting an application: if a section is missing
    here, an ATS reading the same file may miss it too.
    """
    resume = await session.get(Resume, resume_id)
    if resume is None:
        raise ApiError(ErrorCode.NOT_FOUND, "resume not found")

    parsed = resume.parsed_json or {}
    return ResumeParsedOut(
        id=resume.id,
        version=resume.version,
        contact=parsed.get("contact", {}),
        sections={name: len(lines) for name, lines in (parsed.get("sections") or {}).items()},
        line_count=len(parsed.get("raw_lines") or []),
        parsed=parsed,
    )


@router.post("/{resume_id}/edit", response_model=ResumeOut, status_code=status.HTTP_201_CREATED)
async def edit_resume(resume_id: uuid.UUID, body: ResumeEdit, session: SessionDep) -> Resume:
    """Save an edited résumé as a new version, rendered and adopted.

    Until now the parsed form was read-only: fixing a typo, or a section the
    parser mis-split, meant editing the source document elsewhere and
    re-uploading.

    Three things this does that a plain form save would not, each of which is a
    silent wrong answer if skipped.

    **A new version, never a mutation.** An `Application` may already point at
    the source résumé and may already have sent it. Rewriting that row in place
    would leave a receipt describing a document that no longer exists.

    **The PDF is re-rendered from the edit.** `apply_job._resume_path` uploads
    the *file*, while tailoring renders from `parsed_json`. Storing the edit
    without a new file would make it invisible on untailored applications and
    visible on tailored ones — the same divergence that let the base résumé go
    out while the review screen showed a tailored diff.

    **`raw_lines` is rebuilt**, in `packages/tailor/edit.py`. It is what the
    fabrication guard treats as "was this in the source", so an edit that added
    a real employer while leaving it stale would have the guard refuse the
    owner's own fact and the rewriter would look broken.

    With `adopt` (the default) every profile pointing at the source is moved to
    the new version. Without it the edit is stored and nothing uses it, which
    reads as having done nothing.
    """
    source = await session.get(Resume, resume_id)
    if source is None:
        raise ApiError(ErrorCode.NOT_FOUND, "resume not found")
    if not source.parsed_json:
        raise ApiError(ErrorCode.INVALID_REQUEST, "this résumé has no parsed form to edit")

    from packages.tailor.assemble import assemble_pdf
    from packages.tailor.edit import apply_edit
    from packages.tailor.parse import Contact

    # Emptiness is judged on what the editor actually controls, not on the
    # result. `preamble` survives the round trip because no form shows it, so a
    # cleared résumé could still render a PDF containing nothing but a stray
    # "Available from June" — technically non-empty, and not a document anyone
    # meant to send.
    contact_values = [v for v in body.contact.model_dump().values() if v]
    has_sections = any(line.strip() for lines in body.sections.values() for line in lines)
    if not contact_values and not has_sections:
        raise ApiError(ErrorCode.INVALID_REQUEST, "the edited résumé is empty")

    parsed = ParsedResume.model_validate(source.parsed_json)
    edited = apply_edit(
        parsed,
        contact=Contact.model_validate(body.contact.model_dump()),
        sections=body.sections,
    )

    try:
        # Projects are excluded here. They are rebuilt per posting at tailoring
        # time from the GitHub inventory, so baking today's set into the base
        # would freeze a section that is supposed to follow the job.
        pdf = assemble_pdf(edited, None, None)
    except Exception as exc:  # noqa: BLE001 - WeasyPrint needs system libraries
        raise ApiError(
            ErrorCode.INTERNAL_ERROR,
            f"the edit could not be rendered to PDF ({type(exc).__name__}), so it was not "
            "saved — a stored edit with no file would be invisible to every application",
        ) from exc

    next_version = (
        await session.scalar(
            select(func.coalesce(func.max(Resume.version), 0) + 1).where(
                Resume.candidate_id == source.candidate_id
            )
        )
    ) or 1

    storage = get_storage()
    key = resume_key(str(source.candidate_id), next_version, "resume.pdf")
    try:
        storage.put(key, pdf)
    except Exception as exc:  # noqa: BLE001 - surfaces size limits too
        raise ApiError(ErrorCode.INTERNAL_ERROR, f"could not store the edit: {exc}") from exc

    resume = Resume(
        candidate_id=source.candidate_id,
        version=next_version,
        storage_ref=key,
        parsed_json=edited.model_dump(mode="json"),
        is_default=source.is_default,
    )
    session.add(resume)
    await session.flush()

    if body.adopt:
        for profile in (
            await session.scalars(select(Profile).where(Profile.base_resume_id == source.id))
        ).all():
            profile.base_resume_id = resume.id

    await session.commit()
    await session.refresh(resume)
    return resume


@router.post("/{resume_id}/set-base", response_model=ResumeOut)
async def set_as_profile_base(
    resume_id: uuid.UUID, session: SessionDep, profile_id: uuid.UUID
) -> Resume:
    """Point a profile at this résumé as its base for tailoring."""
    resume = await session.get(Resume, resume_id)
    if resume is None:
        raise ApiError(ErrorCode.NOT_FOUND, "resume not found")

    profile = await session.get(Profile, profile_id)
    if profile is None:
        raise ApiError(ErrorCode.INVALID_REQUEST, "profile_id does not exist")

    if profile.candidate_id != resume.candidate_id:
        raise ApiError(ErrorCode.INVALID_REQUEST, "resume belongs to a different candidate")

    profile.base_resume_id = resume.id
    await session.commit()
    await session.refresh(resume)
    return resume


@router.get("/{resume_id}/file")
async def download_resume(resume_id: uuid.UUID, session: SessionDep) -> object:
    """Fetch the stored file. Localhost only — this is PII."""
    from fastapi.responses import FileResponse

    resume = await session.get(Resume, resume_id)
    if resume is None:
        raise ApiError(ErrorCode.NOT_FOUND, "resume not found")

    path = get_storage().path_for(resume.storage_ref)
    if not path.is_file():
        raise ApiError(ErrorCode.NOT_FOUND, "the stored file is missing")

    get_settings()  # keep settings resolution on the same path as storage
    return FileResponse(path, filename=path.name)


@router.post("/{resume_id}/preview", response_model=ResumePreviewOut)
async def preview_assembly(
    resume_id: uuid.UUID, session: SessionDep, job_text: str = "", limit: int = 4
) -> ResumePreviewOut:
    """What an assembled résumé would contain for a given posting.

    Inspectable before anything is sent: which sections survive parsing, which
    projects the ranking picked, and exactly how each link will read.
    """
    resume = await session.get(Resume, resume_id)
    if resume is None:
        raise ApiError(ErrorCode.NOT_FOUND, "resume not found")

    parsed = ParsedResume.model_validate(resume.parsed_json or {})

    projects = list(
        (
            await session.scalars(
                select(Project).where(Project.candidate_id == resume.candidate_id)
            )
        ).all()
    )
    chosen = select_projects(projects, job_text, limit=limit)
    report = describe(parsed, chosen)

    return ResumePreviewOut(
        resume_id=resume.id,
        version=resume.version,
        sections=report.sections,
        project_names=report.project_names,
        source_line_count=report.source_line_count,
        rendered_links=[
            link_text(ProjectEntry.from_project(p), LinkStyle.ICON_SLUG) for p in chosen
        ],
    )
