"""Refresh Studio's public project inventory through the read-only GitHub MCP."""

from __future__ import annotations

from mcp.client import Client

from apps.mcp.github import server
from packages.core.models import Project
from packages.github.live import LiveProjectEvidence, eligible
from packages.llm.provider import LLMError

INVENTORY_LIMIT = 20


async def refresh_projects(projects: list[Project]) -> tuple[list[Project], str]:
    allowed = sorted(
        (project for project in projects if eligible(project)),
        key=lambda project: (not bool(project.pinned), str(project.id)),
    )
    checked: list[Project] = []
    partial = failed = 0
    limited = False
    try:
        async with Client(server) as client:
            for project in allowed[:INVENTORY_LIMIT]:
                result = await client.call_tool(
                    "github_project_evidence",
                    {"project_id": str(project.id)},
                    read_timeout_seconds=65,
                )
                if result.is_error:
                    if any(
                        "GitHub rate limit reached" in getattr(item, "text", "")
                        for item in result.content
                    ):
                        limited = True
                        break
                    failed += 1
                    continue
                payload = result.structured_content or {}
                evidence = LiveProjectEvidence.model_validate(payload.get("result", payload))
                if (
                    evidence.project_id != project.id
                    or evidence.repository_id != project.external_id
                    or evidence.full_name.casefold() != (project.full_name or "").casefold()
                ):
                    raise ValueError("repository identity changed")
                values = {
                    column.key: getattr(project, column.key) for column in Project.__table__.columns
                }
                values.update(
                    name=evidence.name or project.name,
                    description=evidence.description,
                    language=evidence.language,
                    topics_json=[],
                    dependency_technologies=evidence.technologies,
                    url=f"{evidence.url}/tree/{evidence.commit_sha}",
                )
                checked.append(Project(**values))
                partial += not evidence.complete
    except Exception:
        raise LLMError(
            "Live GitHub checking failed. No cached project evidence was substituted."
        ) from None
    if limited:
        raise LLMError(
            "GitHub rate limit reached. Retry after the quota resets or "
            "configure GITHUB_TOKEN on the host. No project draft was generated."
        )
    notice = (
        f"Checked {len(checked)} of {len(allowed)} eligible public repositories via GitHub MCP. "
        f"{failed} failed; {partial} had partial file coverage. "
        "Only successful checks were ranked. Checks cover current default-branch commits."
    )
    if len(allowed) > INVENTORY_LIMIT:
        notice += f" This preview is limited to {INVENTORY_LIMIT} repositories, with pins first."
    return checked, notice
