"""Read saved project declarations for job-first selection. No GitHub client."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Project
from packages.core.models_skill_links import ProjectSkill
from packages.github.select import DEFAULT_LIMIT, projects_for_posting, required_skills
from packages.matching.embed import Embedder


async def saved_declarations(
    session: AsyncSession, projects: Sequence[Project]
) -> dict[uuid.UUID, set[str]]:
    """One batched query; only skill_links' language/dependency edges count."""
    found: dict[uuid.UUID, set[str]] = {project.id: set() for project in projects}
    if found:
        rows = await session.execute(
            select(ProjectSkill.project_id, ProjectSkill.skill).where(
                ProjectSkill.project_id.in_(list(found))
            )
        )
        for project_id, skill in rows:
            found[project_id].add(skill)
    return found


async def saved_projects_for(
    session: AsyncSession,
    candidate_id: uuid.UUID,
    posting_text: str,
    *,
    limit: int = DEFAULT_LIMIT,
    embedder_factory: Callable[[], Embedder] | None = None,
) -> list[Project]:
    """The pipeline's saved-only selection; read no network and write nothing.

    Readable direct requirements need no embedder. Call the original relevance
    fallback's embedder only when requirements cannot establish a ranking.
    """
    projects = list(
        (await session.scalars(select(Project).where(Project.candidate_id == candidate_id))).all()
    )
    if not projects:
        return []
    required = required_skills(posting_text)
    declared = await saved_declarations(session, projects) if required else None
    return projects_for_posting(
        projects,
        posting_text,
        declared=declared,
        limit=limit,
        embedder=embedder_factory() if not required and embedder_factory is not None else None,
    )
