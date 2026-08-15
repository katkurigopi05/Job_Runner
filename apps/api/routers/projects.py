"""Project routes — GitHub sync, listing, and curation."""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import select

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from packages.core.config import get_settings
from packages.core.enums import ErrorCode
from packages.core.models import Candidate, Project
from packages.core.schemas import (
    ProjectOut,
    ProjectPreview,
    ProjectUpdate,
    SyncGitHubRequest,
    SyncResultOut,
)
from packages.github.client import GitHubError, RateLimited
from packages.github.select import score, select_projects
from packages.github.sync import sync_from_github
from packages.tailor.projects import LinkStyle, ProjectEntry, link_text

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("/sync/github", response_model=SyncResultOut)
async def sync_github(body: SyncGitHubRequest, session: SessionDep) -> SyncResultOut:
    """Import the owner's repositories from GitHub.

    Re-running updates in place; `include` and `pinned` choices survive.
    """
    candidate = await session.get(Candidate, body.candidate_id)
    if candidate is None:
        raise ApiError(ErrorCode.INVALID_REQUEST, "candidate_id does not exist")

    token = body.token or get_settings().github_token
    try:
        result = await sync_from_github(
            session,
            candidate.id,
            body.username,
            token=token,
            include_private=body.include_private,
        )
    except RateLimited as exc:
        raise ApiError(ErrorCode.RATE_LIMITED, str(exc)) from exc
    except GitHubError as exc:
        raise ApiError(ErrorCode.INVALID_REQUEST, str(exc)) from exc

    await session.commit()
    return SyncResultOut(added=result.added, updated=result.updated, total=result.total)


@router.get("", response_model=list[ProjectOut])
async def list_projects(session: SessionDep, candidate_id: uuid.UUID) -> list[Project]:
    result = await session.scalars(
        select(Project)
        .where(Project.candidate_id == candidate_id)
        .order_by(Project.pushed_at.desc().nullslast())
    )
    return list(result.all())


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: uuid.UUID, body: ProjectUpdate, session: SessionDep
) -> Project:
    """Curate a project: force it in, force it out, or pin it."""
    project = await session.get(Project, project_id)
    if project is None:
        raise ApiError(ErrorCode.NOT_FOUND, "project not found")

    fields = body.model_dump(exclude_unset=True)
    if "include" in fields:
        project.include = fields["include"]
    if "pinned" in fields:
        project.pinned = fields["pinned"]

    await session.commit()
    await session.refresh(project)
    return project


@router.post("/preview", response_model=list[ProjectPreview])
async def preview_selection(
    session: SessionDep,
    candidate_id: uuid.UUID,
    job_text: str = "",
    limit: int = 4,
    style: LinkStyle = LinkStyle.ICON_SLUG,
) -> list[ProjectPreview]:
    """Which projects would go on a résumé for this posting, and how they read.

    Exists so the ranking is inspectable before it lands on something you send.
    """
    projects = list(
        (await session.scalars(select(Project).where(Project.candidate_id == candidate_id))).all()
    )
    chosen = select_projects(projects, job_text, limit=limit)

    return [
        ProjectPreview(
            id=project.id,
            name=project.name,
            description=project.description,
            url=project.url,
            score=round(score(project, job_text), 4),
            pinned=project.pinned,
            rendered_link=link_text(ProjectEntry.from_project(project), style),
        )
        for project in chosen
    ]
