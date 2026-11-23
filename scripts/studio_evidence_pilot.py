"""Opt-in developer CLI for the resumable LangGraph Studio GitHub-evidence pilot.

Example:
    .venv/bin/python -m scripts.studio_evidence_pilot \\
        --run-id draft-001 \\
        --candidate-id <uuid> \\
        --profile-id <uuid> \\
        --job-description-file posting.txt

The pilot checks eligible GitHub repositories with one durable PostgreSQL
checkpoint per repository and prints the resulting `StudioPreview` JSON on
stdout. It ends at preview; owner approval and commit revalidation remain in
`POST /resumes/synthesize/save`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import cast

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from packages.core.config import get_settings
from packages.core.database_access import async_database_url
from packages.core.schemas_studio import RoleTrackName
from packages.github.client import API_ROOT, GitHubClient
from packages.tailor.studio_pilot import CheckpointEvent, run_studio_evidence_pilot


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run or resume the LangGraph Studio GitHub code-evidence pilot."
    )
    parser.add_argument(
        "--run-id", required=True, help="Stable draft-run ID (LangGraph thread_id)."
    )
    parser.add_argument("--candidate-id", required=True, type=uuid.UUID)
    parser.add_argument("--profile-id", type=uuid.UUID, default=None)
    parser.add_argument("--base-resume-id", type=uuid.UUID, default=None)
    parser.add_argument(
        "--role-track",
        default="swe",
        choices=("swe", "fullstack", "data_ml", "ai_agentic", "cloud_devops", "fde_harness"),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--job-description", help="Raw job description text.")
    group.add_argument("--job-description-file", type=Path, help="Path to a job description file.")
    parser.add_argument(
        "--selected-project-id",
        action="append",
        type=uuid.UUID,
        default=None,
        help="Optional project UUID(s) to pin in order (repeatable).",
    )
    parser.add_argument("--project-limit", type=int, default=4)
    parser.add_argument("--no-internships", action="store_true")
    parser.add_argument(
        "--github-api-root",
        default=API_ROOT,
        help="GitHub REST API root (override for a local stub in tests or dev).",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL URL override (defaults to Settings.database_url).",
    )
    parser.add_argument(
        "--pause-after-checks",
        type=int,
        default=None,
        help=(
            "Emit a checkpoint_committed JSON event on stderr after N repo checks "
            "and wait on stdin."
        ),
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    job_description = (
        args.job_description_file.read_text(encoding="utf-8")
        if args.job_description_file is not None
        else args.job_description
    )
    db_url = args.database_url or settings.database_url
    engine = create_async_engine(async_database_url(db_url), pool_pre_ping=True)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def on_checkpoint(event: CheckpointEvent) -> None:
        if args.pause_after_checks is None or event.completed_checks != args.pause_after_checks:
            return
        sys.stderr.write(
            json.dumps(
                {
                    "event": "checkpoint_committed",
                    "run_id": event.run_id,
                    "checkpoint_id": event.checkpoint_id,
                    "completed_checks": event.completed_checks,
                    "project_id": event.project_id,
                }
            )
            + "\n"
        )
        sys.stderr.flush()
        # Block until the caller either closes stdin to continue or terminates the process.
        await asyncio.to_thread(sys.stdin.readline)

    github = GitHubClient(
        token=settings.github_token,
        api_root=args.github_api_root,
        timeout=3.0,
    )
    try:
        preview = await run_studio_evidence_pilot(
            sessionmaker,
            run_id=args.run_id,
            candidate_id=args.candidate_id,
            profile_id=args.profile_id,
            base_resume_id=args.base_resume_id,
            role_track=cast(RoleTrackName, args.role_track),
            job_description=job_description,
            selected_project_ids=args.selected_project_id,
            include_internships=not args.no_internships,
            project_limit=args.project_limit,
            database_url=db_url,
            github=github,
            on_checkpoint_committed=on_checkpoint if args.pause_after_checks else None,
        )
    finally:
        await engine.dispose()

    sys.stdout.write(preview.model_dump_json(indent=2) + "\n")
    sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
