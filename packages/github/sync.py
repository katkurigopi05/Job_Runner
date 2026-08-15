"""Import repositories into the Project table.

Re-syncing updates rows in place rather than duplicating, keyed on
`(candidate_id, source, external_id)`. The owner's own decisions — `include`
and `pinned` — are never overwritten by a sync; only the facts GitHub reports
are refreshed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Project
from packages.github.client import GitHubClient, Repository

log = structlog.get_logger(__name__)

SOURCE = "github"


@dataclass(frozen=True)
class SyncResult:
    added: int
    updated: int
    total: int


async def sync_repositories(
    session: AsyncSession,
    candidate_id: object,
    repositories: list[Repository],
) -> SyncResult:
    """Upsert repositories for a candidate. Does not commit."""
    existing = {
        project.external_id: project
        for project in (
            await session.scalars(
                select(Project).where(
                    Project.candidate_id == candidate_id, Project.source == SOURCE
                )
            )
        ).all()
    }

    added = 0
    updated = 0
    now = datetime.now(UTC)

    for repository in repositories:
        project = existing.get(repository.external_id)
        if project is None:
            project = Project(
                candidate_id=candidate_id,
                source=SOURCE,
                external_id=repository.external_id,
            )
            session.add(project)
            added += 1
        else:
            updated += 1

        # Facts from GitHub are refreshed every sync.
        project.name = repository.name
        project.full_name = repository.full_name
        project.url = repository.url
        project.homepage = repository.homepage
        project.description = repository.description
        project.language = repository.language
        project.topics_json = list(repository.topics)
        project.stars = repository.stars
        project.forks = repository.forks
        project.is_fork = repository.is_fork
        project.is_archived = repository.is_archived
        project.is_private = repository.is_private
        project.pushed_at = repository.pushed_at
        project.synced_at = now
        # `include` and `pinned` are deliberately untouched — they are the
        # owner's curation, not GitHub's data.

    await session.flush()
    log.info(
        "github_sync",
        candidate_id=str(candidate_id),
        added=added,
        updated=updated,
    )
    return SyncResult(added=added, updated=updated, total=len(repositories))


async def sync_from_github(
    session: AsyncSession,
    candidate_id: object,
    username: str,
    *,
    token: str | None = None,
    include_private: bool = False,
    client: GitHubClient | None = None,
) -> SyncResult:
    """Fetch from GitHub and upsert. Does not commit."""
    github = client or GitHubClient(token=token)
    repositories = await github.list_repositories(username, include_private=include_private)
    return await sync_repositories(session, candidate_id, repositories)
