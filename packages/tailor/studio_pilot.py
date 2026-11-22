"""Opt-in LangGraph pilot for resumable Resume Studio GitHub code-evidence checks.

Scope and boundaries:
- Orchestrates Resume Studio's live GitHub code-evidence checks with one
  durable PostgreSQL checkpoint boundary per repository (`durability="sync"`).
- Reuses `packages.tailor.code_evidence.extract_code_requirements`,
  `packages.github.code_search.{plan_code_search, scan_repository, finalize_search_result}`,
  and `packages.tailor.studio_preview.assemble_live_code_preview`.
- Ends at `StudioPreview`. Owner approval and live commit revalidation remain
  solely in `apps/api/routers/resume_studio.py::save_draft`.

Privacy and storage controls:
- Credentials (`GitHubClient` / token, database URL), raw job-description text,
  `ParsedResume`, and source-file contents are passed only in the in-memory
  runtime context (`PilotRuntime`), never in graph state or `RunnableConfig`.
- Persisted checkpoint state holds only project UUIDs, progress cursor, compact
  `CodeFinding` citations (fixed rule explanations, no raw source excerpts, and
  `permalink=None` for private repos), and fixed limitation strings.
- LangGraph tables live in a dedicated schema (`langgraph_studio_pilot`) so
  Job_Runner's `public` schema and Alembic migrations remain untouched.

Replay boundary:
- Each repository check commits synchronously to PostgreSQL at the end of its
  superstep before the next repository begins.
- A repository whose checkpoint was durably committed is skipped on restart or
  retry. A call that finished just before its checkpoint committed, or raised a
  transient error before commit, is replayable and will run again on resume.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

import psycopg
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from psycopg import sql
from psycopg.rows import dict_row
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.config import get_settings
from packages.core.models import Project, Resume
from packages.core.schemas_studio import RoleTrackName, StudioPreview
from packages.github.client import GitHubClient
from packages.github.code_search import (
    CodeFinding,
    RepoScanOutcome,
    SearchResult,
    finalize_search_result,
    plan_code_search,
    scan_repository,
)
from packages.matching.requirements import SkillMention
from packages.tailor.code_evidence import extract_code_requirements
from packages.tailor.parse import ParsedResume
from packages.tailor.studio import RoleTrack
from packages.tailor.studio_load import (
    candidate_projects,
    resolve_base,
    resolve_profile,
    studio_source,
)
from packages.tailor.studio_preview import assemble_live_code_preview
from packages.tailor.studio_sources import Source

#: Dedicated Postgres schema for LangGraph's 4 checkpointer tables so removing
#: the pilot is `DROP SCHEMA langgraph_studio_pilot CASCADE` with zero Alembic drift.
PILOT_CHECKPOINT_SCHEMA = "langgraph_studio_pilot"


def psycopg_database_url(url: str) -> str:
    """Normalize a SQLAlchemy or libpq PostgreSQL URL for psycopg 3."""
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql://", 1)
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "postgresql://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


@dataclass(frozen=True)
class CheckpointEvent:
    """Emitted immediately after a repository check checkpoint commits to Postgres."""

    run_id: str
    checkpoint_id: str
    completed_checks: int
    project_id: str


CheckpointCallback = Callable[[CheckpointEvent], Awaitable[None]]


class PilotState(TypedDict, total=False):
    """Compact, non-PII state persisted across LangGraph checkpoints."""

    planned: bool
    selected_project_ids: list[str]
    initial_limitations: list[str]
    next_index: int
    repo_outcomes: list[dict[str, Any]]
    finalized: bool
    final_findings: list[dict[str, Any]]
    final_limitations: list[str]
    searched: int
    chosen_project_ids: list[str]
    completed: bool


@dataclass
class PilotRuntime:
    """Non-persisted runtime dependencies and PII inputs for one invocation."""

    base_row: Resume
    base: ParsedResume
    source: Source
    job_description: str
    track: RoleTrack
    projects: list[Project]
    projects_by_id: dict[str, Project]
    needs: list[SkillMention]
    github: GitHubClient
    selected_project_ids: list[uuid.UUID] | None
    project_limit: int
    preview: StudioPreview | None = None


class _SignalingPostgresSaver(AsyncPostgresSaver):
    """AsyncPostgresSaver that optionally notifies once a repo check commits."""

    def __init__(
        self,
        conn: psycopg.AsyncConnection[dict[str, Any]],
        *,
        on_checkpoint_committed: CheckpointCallback | None = None,
    ) -> None:
        super().__init__(conn)
        self._on_checkpoint_committed = on_checkpoint_committed
        self._reported_checks = 0

    def set_initial_completed_checks(self, count: int) -> None:
        self._reported_checks = count

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        next_config = await super().aput(config, checkpoint, metadata, new_versions)
        if self._on_checkpoint_committed is not None and metadata.get("source") == "loop":
            channel_values = checkpoint.get("channel_values") or {}
            outcomes = channel_values.get("repo_outcomes")
            if isinstance(outcomes, list) and len(outcomes) > self._reported_checks:
                self._reported_checks = len(outcomes)
                last_project_id = str(outcomes[-1].get("project_id", ""))
                thread_id = str(config.get("configurable", {}).get("thread_id", ""))
                await self._on_checkpoint_committed(
                    CheckpointEvent(
                        run_id=thread_id,
                        checkpoint_id=str(checkpoint["id"]),
                        completed_checks=self._reported_checks,
                        project_id=last_project_id,
                    )
                )
        return next_config


def _serialize_outcome(project_id: uuid.UUID, outcome: RepoScanOutcome) -> dict[str, Any]:
    return {
        "project_id": str(project_id),
        "searched": outcome.searched,
        "limitations": list(outcome.limitations),
        "findings": [finding.model_dump(mode="json") for finding in outcome.findings],
    }


def _deserialize_outcome(raw: Mapping[str, Any]) -> RepoScanOutcome:
    return RepoScanOutcome(
        findings=tuple(CodeFinding.model_validate(item) for item in raw.get("findings", ())),
        limitations=tuple(str(item) for item in raw.get("limitations", ())),
        searched=int(raw.get("searched", 0)),
    )


def _search_result_from_state(state: Mapping[str, Any]) -> SearchResult:
    return SearchResult(
        findings=[CodeFinding.model_validate(item) for item in state.get("final_findings", ())],
        limitations=[str(item) for item in state.get("final_limitations", ())],
        searched=int(state.get("searched", 0)),
    )


async def _select_repositories(state: PilotState, runtime: Runtime[PilotRuntime]) -> dict[str, Any]:
    ctx = runtime.context
    plan = plan_code_search(
        ctx.projects,
        ctx.needs,
        allow_private=bool(ctx.github.token),
    )
    return {
        "planned": True,
        "selected_project_ids": [str(project.id) for project in plan.candidates],
        "initial_limitations": list(plan.limitations),
        "next_index": 0,
        "repo_outcomes": [],
    }


async def _check_repository(state: PilotState, runtime: Runtime[PilotRuntime]) -> dict[str, Any]:
    ctx = runtime.context
    selected = state.get("selected_project_ids", [])
    index = state.get("next_index", 0)
    project_id = selected[index]
    project = ctx.projects_by_id.get(project_id)
    if project is None:
        outcome = RepoScanOutcome(
            limitations=("At least one GitHub repository could not be fully checked.",)
        )
        record = _serialize_outcome(uuid.UUID(project_id), outcome)
    else:
        outcome = await scan_repository(
            project,
            ctx.needs,
            github=ctx.github,
            raise_transient=True,
        )
        record = _serialize_outcome(project.id, outcome)
    # Deduplicate by project_id so replaying an uncommitted step never duplicates entries.
    existing = [
        item for item in state.get("repo_outcomes", []) if item.get("project_id") != project_id
    ]
    return {
        "repo_outcomes": [*existing, record],
        "next_index": index + 1,
    }


async def _finalize_evidence(state: PilotState, runtime: Runtime[PilotRuntime]) -> dict[str, Any]:
    outcomes = [_deserialize_outcome(item) for item in state.get("repo_outcomes", ())]
    result = finalize_search_result(outcomes, state.get("initial_limitations", ()))
    return {
        "finalized": True,
        "final_findings": [finding.model_dump(mode="json") for finding in result.findings],
        "final_limitations": list(result.limitations),
        "searched": result.searched,
    }


async def _assemble_preview(state: PilotState, runtime: Runtime[PilotRuntime]) -> dict[str, Any]:
    ctx = runtime.context
    result = _search_result_from_state(state)
    preview = await assemble_live_code_preview(
        base_row=ctx.base_row,
        base=ctx.base,
        source=ctx.source,
        job_description=ctx.job_description,
        track=ctx.track,
        projects=ctx.projects,
        code=result,
        selected_project_ids=ctx.selected_project_ids,
        project_limit=ctx.project_limit,
        provider=None,
    )
    # Keep the rendered preview (résumé prose/HTML/PII) in runtime memory only.
    ctx.preview = preview
    return {
        "chosen_project_ids": [str(project.id) for project in preview.projects if project.chosen],
        "completed": True,
    }


def _next_step(state: PilotState) -> Literal["check_repository", "finalize_evidence"]:
    selected = state.get("selected_project_ids", [])
    index = state.get("next_index", 0)
    return "check_repository" if index < len(selected) else "finalize_evidence"


def build_pilot_graph(checkpointer: AsyncPostgresSaver) -> Any:
    """Compile the four-node resumable GitHub evidence graph."""
    builder = StateGraph(PilotState, context_schema=PilotRuntime)
    builder.add_node("select_repositories", _select_repositories)
    builder.add_node("check_repository", _check_repository)
    builder.add_node("finalize_evidence", _finalize_evidence)
    builder.add_node("assemble_preview", _assemble_preview)

    builder.add_edge(START, "select_repositories")
    builder.add_conditional_edges(
        "select_repositories",
        _next_step,
        {"check_repository": "check_repository", "finalize_evidence": "finalize_evidence"},
    )
    builder.add_conditional_edges(
        "check_repository",
        _next_step,
        {"check_repository": "check_repository", "finalize_evidence": "finalize_evidence"},
    )
    builder.add_edge("finalize_evidence", "assemble_preview")
    builder.add_edge("assemble_preview", END)
    return builder.compile(checkpointer=checkpointer)


@dataclass(frozen=True)
class PilotInputs:
    """Immutable Studio inputs loaded in a short read transaction before graph execution."""

    base_row: Resume
    base: ParsedResume
    source: Source
    projects: list[Project]


async def load_pilot_inputs(
    session: AsyncSession,
    *,
    candidate_id: uuid.UUID,
    profile_id: uuid.UUID | None = None,
    base_resume_id: uuid.UUID | None = None,
    selected_certification_ids: Sequence[uuid.UUID] | None = None,
    include_internships: bool = True,
) -> PilotInputs:
    """Load and detach Studio inputs so no SQLAlchemy transaction stays open during I/O."""
    profile = await resolve_profile(session, candidate_id, profile_id)
    base_row = await resolve_base(session, candidate_id, base_resume_id, profile)
    base = ParsedResume.model_validate(base_row.parsed_json)
    source = await studio_source(
        session,
        candidate_id,
        base,
        include_internships=include_internships,
        certification_ids=selected_certification_ids,
    )
    projects = await candidate_projects(session, candidate_id)
    session.expunge(base_row)
    for project in projects:
        session.expunge(project)
    return PilotInputs(base_row=base_row, base=base, source=source, projects=projects)


async def run_studio_evidence_pilot(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    run_id: str,
    candidate_id: uuid.UUID,
    job_description: str,
    profile_id: uuid.UUID | None = None,
    base_resume_id: uuid.UUID | None = None,
    role_track: RoleTrackName = "swe",
    selected_project_ids: Sequence[uuid.UUID] | None = None,
    selected_certification_ids: Sequence[uuid.UUID] | None = None,
    include_internships: bool = True,
    project_limit: int = 4,
    database_url: str | None = None,
    github: GitHubClient | None = None,
    schema: str = PILOT_CHECKPOINT_SCHEMA,
    on_checkpoint_committed: CheckpointCallback | None = None,
) -> StudioPreview:
    """Run or resume a durable LangGraph GitHub code-evidence preview for `run_id`."""
    if not run_id.strip():
        raise ValueError("run_id must be a non-empty stable identifier")

    settings = get_settings()
    # Close the SQLAlchemy read transaction before opening psycopg and running
    # `AsyncPostgresSaver.setup()`, whose `CREATE INDEX CONCURRENTLY` waits on
    # any open transaction in the same database.
    async with sessionmaker() as session:
        inputs = await load_pilot_inputs(
            session,
            candidate_id=candidate_id,
            profile_id=profile_id,
            base_resume_id=base_resume_id,
            selected_certification_ids=selected_certification_ids,
            include_internships=include_internships,
        )
    track = RoleTrack(role_track)
    needs = extract_code_requirements(job_description)
    active_github = github or GitHubClient(token=settings.github_token, timeout=3.0)

    runtime = PilotRuntime(
        base_row=inputs.base_row,
        base=inputs.base,
        source=inputs.source,
        job_description=job_description,
        track=track,
        projects=inputs.projects,
        projects_by_id={str(project.id): project for project in inputs.projects},
        needs=needs,
        github=active_github,
        selected_project_ids=None if selected_project_ids is None else list(selected_project_ids),
        project_limit=project_limit,
    )

    conn_string = psycopg_database_url(database_url or settings.database_url)
    async with await psycopg.AsyncConnection.connect(
        conn_string, autocommit=True, prepare_threshold=0, row_factory=dict_row
    ) as conn:
        await conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))

        checkpointer = _SignalingPostgresSaver(
            conn, on_checkpoint_committed=on_checkpoint_committed
        )
        await checkpointer.setup()
        graph = build_pilot_graph(checkpointer)

        # Only the stable thread_id enters RunnableConfig; never pass secrets or prose here.
        config: RunnableConfig = {"configurable": {"thread_id": run_id}}
        snapshot = await graph.aget_state(config)
        if snapshot.values and not snapshot.next:
            result = _search_result_from_state(snapshot.values)
            return await assemble_live_code_preview(
                base_row=runtime.base_row,
                base=runtime.base,
                source=runtime.source,
                job_description=runtime.job_description,
                track=runtime.track,
                projects=runtime.projects,
                code=result,
                selected_project_ids=runtime.selected_project_ids,
                project_limit=runtime.project_limit,
                provider=None,
            )

        existing_outcomes = snapshot.values.get("repo_outcomes") if snapshot.values else []
        if isinstance(existing_outcomes, list):
            checkpointer.set_initial_completed_checks(len(existing_outcomes))

        graph_input: PilotState | None = None if snapshot.next else {}
        await graph.ainvoke(
            graph_input,
            config=config,
            context=runtime,
            durability="sync",
        )
        if runtime.preview is None:
            raise RuntimeError("LangGraph pilot finished without assembling a Studio preview.")
        return runtime.preview
