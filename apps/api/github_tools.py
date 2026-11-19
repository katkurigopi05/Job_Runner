"""A single bounded Kaggle tool round through the read-only GitHub MCP server."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from mcp.client import Client
from mcp.types import CallToolResult, ListToolsResult
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.mcp.github import server
from packages.core.models import Candidate, Project
from packages.github.live import LiveProjectEvidence, eligible
from packages.llm.provider import LLMError
from packages.matching.skill_vocab import normalize_skill

MAX_PROJECTS = 5
CATALOG_LIMIT = 20
TOOL_NAME = "github_project_evidence"


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    project_id: str


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Literal["github_project_evidence"]
    arguments: ToolArguments


class ToolPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tool_calls: Annotated[list[ToolCall], Field(min_length=1, max_length=MAX_PROJECTS)]


@dataclass(frozen=True)
class LiveProjects:
    projects: tuple[Project, ...]
    notice: str
    model_used: bool = False
    brief: str = ""


def parse_plan(raw: str, allowed: set[str]) -> ToolPlan:
    if not isinstance(raw, str) or len(raw) > 16384:
        raise LLMError("Kaggle did not return valid GitHub tool requests.")
    # The live Kaggle helper can wrap otherwise valid JSON in one Markdown
    # code block. Remove only that complete wrapper; never extract a JSON
    # substring from prose, repair arguments or weaken the tool allowlist.
    cleaned = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", cleaned, re.I | re.S)
    if fenced:
        cleaned = fenced.group(1)
    try:
        plan = ToolPlan.model_validate_json(cleaned)
    except (ValidationError, ValueError):
        raise LLMError("Kaggle did not return valid GitHub tool requests.") from None
    ids = [call.arguments.project_id for call in plan.tool_calls]
    if len(set(ids)) != len(ids) or not set(ids) <= allowed:
        raise LLMError("Kaggle requested a project outside the allowed GitHub evidence scope.")
    return plan


async def live_projects_for_graph(
    session: AsyncSession, question: str, provider: Any
) -> LiveProjects:
    candidates = list((await session.scalars(select(Candidate.id).limit(2))).all())
    if len(candidates) != 1:
        return LiveProjects((), "Live GitHub checking needs exactly one candidate.")
    imported = list(
        (
            await session.scalars(
                select(Project)
                .where(Project.candidate_id == candidates[0])
                .order_by(Project.pushed_at.desc().nullslast(), Project.id)
            )
        ).all()
    )
    allowed_projects = [project for project in imported if eligible(project)]
    catalog = allowed_projects[:CATALOG_LIMIT]
    if not catalog:
        return LiveProjects(
            (), "No eligible imported public GitHub projects are available to check."
        )
    allowed = {str(project.id): project for project in catalog}
    discovered = await _discover_tools()
    tools = [tool for tool in discovered.tools if tool.name == TOOL_NAME]
    if len(tools) != 1:
        raise LLMError("The read-only GitHub evidence tool is unavailable.")
    tool = tools[0]
    raw = await provider.complete(
        "You may request live GitHub project evidence before answering a project-matching "
        "question. Return only JSON matching the given response_schema, with no Markdown "
        "or code fences. Request between "
        "one and five distinct catalog project IDs using github_project_evidence. "
        "The host executes these read-only MCP tools. Catalog skills are cached hints, "
        "not current evidence. You cannot request URLs, tokens, writes or other tools.",
        json.dumps(
            {
                "question": question,
                "tool": {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                },
                "response_schema": ToolPlan.model_json_schema(),
                "projects": [
                    {
                        "project_id": str(project.id),
                        "alias": f"project {n + 1}",
                        "cached_skills": sorted(
                            {
                                key
                                for name in [
                                    project.language or "",
                                    *(project.dependency_technologies or []),
                                ]
                                if (key := normalize_skill(name)) is not None
                            }
                        ),
                    }
                    for n, project in enumerate(catalog)
                ],
            }
        ),
        max_tokens=512,
        temperature=0.0,
    )
    plan = parse_plan(raw, set(allowed))
    refreshed = []
    checks = []
    failed = partial = 0
    results = await _execute_tools(plan)
    for call, result in zip(plan.tool_calls, results, strict=True):
        if result.is_error:
            failed += 1
            continue
        payload = result.structured_content or {}
        if "result" in payload:
            payload = payload["result"]
        try:
            evidence = LiveProjectEvidence.model_validate(payload)
        except (ValidationError, ValueError):
            raise LLMError("GitHub MCP returned evidence without verifiable provenance.") from None
        original = allowed[call.arguments.project_id]
        if (
            evidence.project_id != original.id
            or evidence.repository_id != original.external_id
            or evidence.full_name.casefold() != (original.full_name or "").casefold()
        ):
            raise LLMError("GitHub MCP returned evidence for a different repository.")
        refreshed.append(
            Project(
                id=original.id,
                candidate_id=original.candidate_id,
                source="github",
                external_id=original.external_id,
                name=original.name,
                full_name=evidence.full_name,
                url=f"{evidence.url}/tree/{evidence.commit_sha}",
                language=evidence.language,
                dependency_technologies=evidence.technologies,
                is_private=False,
                is_fork=False,
                include=original.include,
            )
        )
        partial += not evidence.complete
        checks.append(
            f"project {list(allowed).index(str(evidence.project_id)) + 1}: "
            f"commit {evidence.commit_sha}, checked {evidence.checked_at.isoformat()}"
        )
    notice = (
        f"Live GitHub MCP checked {len(refreshed)} of {len(allowed_projects)} eligible imported "
        f"public projects; {failed} checks failed, {partial} have partial file coverage. "
        "Only successfully checked projects contribute technology evidence to this answer; "
        "unchecked or failed projects were not replaced by cached facts. "
        "Checks cover current default-branch commits, not local unpushed changes. "
        + " ".join(checks)
    )
    brief = f"Checked {len(refreshed)} of {len(allowed_projects)} public GitHub projects just now."
    if partial:
        brief += f" {partial} had partial file coverage."
    if failed:
        brief += f" {failed} checks failed; those projects were left out."
    return LiveProjects(tuple(refreshed), notice, model_used=True, brief=brief)


async def _discover_tools() -> ListToolsResult:
    try:
        async with Client(server) as client:
            return await client.list_tools()
    except Exception:
        raise LLMError("The read-only GitHub MCP server is unavailable.") from None


async def _execute_tools(plan: ToolPlan) -> list[CallToolResult]:
    try:
        async with Client(server) as client:
            return [
                await client.call_tool(
                    TOOL_NAME, {"project_id": call.arguments.project_id}, read_timeout_seconds=65
                )
                for call in plan.tool_calls
            ]
    except Exception:
        raise LLMError(
            "Live GitHub MCP checking failed; no cached facts were substituted."
        ) from None
