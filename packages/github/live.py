"""Bounded, commit-pinned GitHub evidence for imported public projects."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config import get_settings
from packages.core.models import Project
from packages.github.client import (
    MAX_EVIDENCE_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_MANIFESTS,
    RAW_ROOT,
    GitHubClient,
    GitHubError,
    Repository,
)
from packages.github.infrastructure import declared_in, is_infrastructure_path
from packages.github.manifests import is_manifest_path, packages_in, technologies

_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")
_SHA = r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$"


class LiveProjectEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: uuid.UUID
    repository_id: str
    full_name: str
    url: str
    commit_sha: str = Field(pattern=_SHA)
    checked_at: datetime
    language: str | None
    technologies: list[str]
    files_read: list[str]
    complete: bool
    name: str = ""
    description: str | None = None


def eligible(project: Project) -> bool:
    return (
        project.source == "github"
        and not project.is_fork
        and not project.is_private
        and project.include is not False
        and project.full_name is not None
        and _REPOSITORY.fullmatch(project.full_name) is not None
        and project.full_name.rsplit("/", 1)[-1] not in {".", ".."}
    )


async def _read_file(client: httpx.AsyncClient, url: str, limit: int) -> tuple[str | None, int]:
    async with client.stream("GET", url) as response:
        if response.status_code != 200:
            return None, 0
        content = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=16384):
            content.extend(chunk)
            if len(content) > limit:
                return None, len(content)
        return content.decode("utf-8", errors="replace"), len(content)


async def read_live_project(
    session: AsyncSession, project_id: uuid.UUID, *, github: GitHubClient | None = None
) -> LiveProjectEvidence:
    project = await session.get(Project, project_id)
    if project is None or not eligible(project) or project.full_name is None:
        raise GitHubError("Only imported, included public non-fork GitHub projects may be checked.")
    github = github or GitHubClient(token=get_settings().github_token)
    try:
        async with httpx.AsyncClient(
            transport=github._transport,
            timeout=github._timeout,
            headers=github._headers(),
            follow_redirects=False,
        ) as client:
            base = f"{github.api_root}/repos/{project.full_name}"
            response = await client.get(base)
            github._raise_for_status(response)
            metadata = response.json()
            if metadata.get("private") is not False or metadata.get("fork") is not False:
                raise GitHubError("GitHub repository visibility or ownership cannot be verified.")
            repository = Repository.from_api(metadata)
            if (
                repository.external_id != project.external_id
                or repository.full_name.casefold() != project.full_name.casefold()
                or repository.is_private
                or repository.is_fork
            ):
                raise GitHubError(
                    "GitHub repository identity or visibility changed; sync projects."
                )
            branch = quote(repository.default_branch or "HEAD", safe="")
            response = await client.get(f"{base}/commits/{branch}")
            github._raise_for_status(response)
            commit = response.json().get("sha")
            if not isinstance(commit, str) or not re.fullmatch(_SHA, commit):
                raise GitHubError("GitHub did not return a verifiable commit.")
            response = await client.get(f"{base}/git/trees/{commit}", params={"recursive": "1"})
            github._raise_for_status(response)
            listing = response.json()
            if not isinstance(listing, dict) or not isinstance(listing.get("tree"), list):
                raise GitHubError("GitHub did not return a readable repository tree.")
            paths = sorted(
                {
                    entry["path"]
                    for entry in listing["tree"]
                    if isinstance(entry, dict)
                    and entry.get("type") == "blob"
                    and isinstance(entry.get("path"), str)
                    and not any(part in {".", "..", ""} for part in entry["path"].split("/"))
                    and (is_manifest_path(entry["path"]) or is_infrastructure_path(entry["path"]))
                },
                key=lambda path: (path.count("/"), path),
            )
            complete = not listing.get("truncated", False) and len(paths) <= MAX_MANIFESTS
            declared: set[str] = set()
            named: set[str] = set()
            files = []
            remaining = MAX_EVIDENCE_BYTES
            for path in paths[:MAX_MANIFESTS]:
                if remaining <= 0:
                    complete = False
                    break
                url = f"{RAW_ROOT}/{repository.full_name}/{commit}/{quote(path, safe='/')}"
                content, consumed = await _read_file(
                    client, url, min(MAX_MANIFEST_BYTES, remaining)
                )
                remaining -= consumed
                if content is None:
                    complete = False
                    continue
                files.append(path)
                if is_infrastructure_path(path):
                    named |= declared_in(path, content)
                else:
                    declared |= packages_in(path, content)
            return LiveProjectEvidence(
                project_id=project.id,
                repository_id=repository.external_id,
                full_name=repository.full_name,
                url=f"https://github.com/{repository.full_name}",
                commit_sha=commit,
                checked_at=datetime.now(UTC),
                name=repository.name,
                description=repository.description,
                language=repository.language,
                technologies=sorted({*technologies(declared), *named}, key=str.lower),
                files_read=files,
                complete=complete,
            )
    except GitHubError:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        raise GitHubError(
            "GitHub project evidence could not be verified; no cached facts used."
        ) from None
