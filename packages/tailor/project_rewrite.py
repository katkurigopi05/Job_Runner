"""Job-specific project prose, kept separate from the imported source record."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from packages.core.models import Project
from packages.llm.provider import LLMProvider
from packages.tailor.evidence import repository_technologies_text
from packages.tailor.guard import SourceCorpus
from packages.tailor.keywords import analyze
from packages.tailor.rewrite import tailor_bullet, vet


@dataclass(frozen=True)
class ProjectRewrite:
    project_id: uuid.UUID
    original: str
    tailored: str
    status: str
    reason: str | None = None


def corpus_for(project: Project) -> SourceCorpus:
    # Only THIS project's description and declarations. No other project or
    # employer can lend it a feature, metric, technology or achievement.
    return SourceCorpus.from_texts(project.description or "", repository_technologies_text(project))


def validate_description(project: Project, original: str, tailored: str) -> str | None:
    if project.is_fork:
        return "Forked projects are excluded from project tailoring."
    if original != (project.description or ""):
        return "The source description changed. Generate a new preview."
    if tailored == original:
        return None
    if not original.strip():
        return "A project without a source description cannot have a generated description."
    accepted, reason, _ = vet(original, tailored, corpus_for(project))
    return None if accepted else reason or "Unsupported project description."


def with_description(project: Project, description: str) -> Project:
    """A transient copy; assigning to an ORM-loaded Project would dirty the source."""
    return Project(
        **{
            column.key: (
                description if column.key == "description" else getattr(project, column.key)
            )
            for column in Project.__table__.columns
        }
    )


async def rewrite_project(
    provider: LLMProvider, project: Project, job_description: str
) -> ProjectRewrite:
    original = project.description or ""
    if project.is_fork:
        return ProjectRewrite(project.id, original, original, "excluded fork")
    if not original.strip():
        return ProjectRewrite(project.id, original, original, "no source description")
    corpus = corpus_for(project)
    result = await tailor_bullet(
        provider, original, job_description, corpus, analyze(job_description, corpus)
    )
    status = (
        "provider failed"
        if result.provider_failed
        else "refused"
        if result.rejected_reason
        else "rewritten"
        if result.changed
        else "kept"
    )
    return ProjectRewrite(project.id, original, result.tailored, status, result.rejected_reason)
