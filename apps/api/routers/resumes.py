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
from packages.core.models import Candidate, Profile, Resume
from packages.core.schemas import ResumeOut, ResumeParsedOut
from packages.core.storage import get_storage, resume_key
from packages.tailor.parse import ParseError, parse_resume

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
