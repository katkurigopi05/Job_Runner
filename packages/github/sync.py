"""Import repositories into the Project table.

Re-syncing updates rows in place rather than duplicating, keyed on
`(candidate_id, source, external_id)`. The owner's own decisions — `include`,
`pinned` and `resume_entry` — are never overwritten by a sync; only the facts
GitHub reports are refreshed.

A sync also reads what each repository's own dependency files declare
(`packages/github/manifests.py`), for the owner's repositories only and only
after a new push. A fork is someone else's code until the owner commits to
it, and nothing here can tell which of its dependencies the owner added.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Project
from packages.github.client import GitHubClient, GitHubError, RateLimited, Repository

log = structlog.get_logger(__name__)

SOURCE = "github"


@dataclass(frozen=True)
class SyncResult:
    added: int
    updated: int
    total: int
    #: Repositories whose dependency files were read this sync.
    dependencies_read: int = 0
    #: Repositories due a read that did not get one: a rate limit, or GitHub
    #: refusing one repository. The next sync tries them again.
    dependencies_skipped: int = 0


def repos_to_read(
    repositories: Sequence[Repository], last_read: Mapping[str, datetime | None]
) -> list[Repository]:
    """The owner's repositories pushed to since their dependency files were last read.

    `last_read` holds the push a repository was read at, by external id. Not
    in it means never read.
    """
    return [
        repository
        for repository in repositories
        if not repository.is_fork
        and (
            repository.external_id not in last_read
            or last_read[repository.external_id] != repository.pushed_at
        )
    ]


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


async def read_dependency_evidence(
    session: AsyncSession,
    candidate_id: object,
    repositories: Sequence[Repository],
    client: GitHubClient,
) -> tuple[int, int]:
    """Read dependency files for repositories pushed to since the last read.

    Returns (read, skipped). A rate limit stops the pass and leaves the rest
    for the next sync rather than recording "uses nothing" for them. Does not
    commit.
    """
    projects = {
        project.external_id: project
        for project in (
            await session.scalars(
                select(Project).where(
                    Project.candidate_id == candidate_id, Project.source == SOURCE
                )
            )
        ).all()
    }
    last_read = {
        external_id: project.dependencies_pushed_at
        for external_id, project in projects.items()
        if project.dependencies_pushed_at is not None
    }
    due = [r for r in repos_to_read(repositories, last_read) if r.external_id in projects]

    read = 0
    for position, repository in enumerate(due):
        try:
            found = await client.repository_technologies(repository)
        except RateLimited:
            log.warning("github_dependencies_rate_limited", remaining=len(due) - position)
            return read, len(due) - position
        except GitHubError:
            continue
        project = projects[repository.external_id]
        project.dependency_technologies = found
        project.dependencies_pushed_at = repository.pushed_at
        read += 1

    await session.flush()
    return read, len(due) - read


async def sync_from_github(
    session: AsyncSession,
    candidate_id: object,
    username: str,
    *,
    token: str | None = None,
    include_private: bool = False,
    client: GitHubClient | None = None,
) -> SyncResult:
    """Fetch from GitHub, upsert, and read new dependency files. Does not commit."""
    github = client or GitHubClient(token=token)
    repositories = await github.list_repositories(username, include_private=include_private)
    result = await sync_repositories(session, candidate_id, repositories)
    read, skipped = await read_dependency_evidence(session, candidate_id, repositories, github)
    return SyncResult(
        added=result.added,
        updated=result.updated,
        total=result.total,
        dependencies_read=read,
        dependencies_skipped=skipped,
    )
