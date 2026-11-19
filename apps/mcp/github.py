"""A narrow MCP server for live, read-only project evidence. No GitHub writes."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from packages.core import db as core_db
from packages.github.client import GitHubError, RateLimited
from packages.github.live import read_live_project

server = MCPServer(name="jobrunner-github")


@server.tool()
async def github_project_evidence(project_id: UUID) -> dict[str, Any]:
    """Check an imported public GitHub project now, reading declarations at its current commit.

    Returns commit SHA, check time, primary language, declared technologies,
    evidence file paths and coverage. Private, forked and excluded repositories
    are refused. GitHub credentials stay on the host. This tool never writes.
    """
    try:
        async with asyncio.timeout(60):
            async with core_db.get_sessionmaker()() as session:
                result = await read_live_project(session, project_id)
                return result.model_dump(mode="json")
    except RateLimited:
        raise ToolError(
            "GitHub rate limit reached. Retry after the quota resets or configure "
            "GITHUB_TOKEN on the host. No cached evidence was substituted."
        ) from None
    except (GitHubError, TimeoutError):
        raise ToolError(
            "Live GitHub evidence is unavailable; cached evidence was not substituted."
        ) from None


if __name__ == "__main__":
    server.run()
