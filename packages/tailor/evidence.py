"""Verified GitHub evidence that may appear in a tailored résumé.

Repository names, descriptions, primary languages, topics, and what the
repository's own dependency files declare are copied from GitHub by the sync
(`packages/github`). They are useful evidence for a skill the source résumé
omitted, but only when they remain attributed to that project. This module
never reads an employer bullet and never generates a claim.

A repository the owner linked to a résumé Projects entry (`Project.resume_entry`)
is evidence for that entry's bullets (`linked_evidence`, CLAUDE.md §2.1).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Project
from packages.tailor.guard import SourceCorpus, entry_key
from packages.tailor.keywords import analyze


def project_source_text(project: Project) -> str:
    """Flatten only source-reported project fields for matching and audit."""
    return "\n".join(
        part
        for part in (
            project.name,
            project.description or "",
            project.language or "",
            "\n".join(project.topics_json or []),
            "\n".join(project.dependency_technologies or []),
        )
        if part.strip()
    )


def matched_job_terms(project: Project, job_description: str) -> list[str]:
    """Salient posting terms directly supported by one GitHub project."""
    if not job_description.strip():
        return []
    evidence = SourceCorpus.from_texts(project_source_text(project))
    return analyze(job_description, evidence).supported


def repository_technologies_text(project: Project) -> str:
    """A linked repository's technology facts: primary language, topics, declared tools.

    Not its name or description. A description is prose, and its words are
    ordinary ones: measured over 400 open postings, linking the owner's
    repositories by their full source text made "features", "fix", "review"
    and "foundational" the terms most often offered for a bullet. A
    description still prints under its project in a generated Projects
    section (`project_source_text`); it is not a reason to rewrite a bullet.
    """
    return "\n".join(
        part
        for part in (
            project.language or "",
            *(project.topics_json or []),
            *(project.dependency_technologies or []),
        )
        if part.strip()
    )


async def linked_evidence(session: AsyncSession, candidate_id: uuid.UUID) -> dict[str, str]:
    """Technology facts per résumé Projects entry, from the repositories linked to it.

    A fork is left out even when linked: it is someone else's code until the
    owner commits to it, and the sync records nothing to say they did.
    """
    projects = (
        await session.scalars(
            select(Project)
            .where(
                Project.candidate_id == candidate_id,
                Project.resume_entry.is_not(None),
                Project.is_fork.is_(False),
            )
            .order_by(Project.name)
        )
    ).all()
    grouped: dict[str, list[str]] = {}
    for project in projects:
        key = entry_key(project.resume_entry or "")
        text = repository_technologies_text(project)
        if key and text:
            grouped.setdefault(key, []).append(text)
    return {key: "\n".join(texts) for key, texts in grouped.items()}


def _comparable(name: str) -> str:
    """`Job_Runner` and `jobrunner` compare equal; so do `Attorney.AI` and `attorney-ai`."""
    return "".join(ch for ch in name.casefold() if ch.isalnum())


def suggest_links(names: list[str], projects: list[Project]) -> dict[uuid.UUID, str]:
    """Résumé entry names a repository plainly is, by name alone.

    Only an exact match after case and punctuation are dropped: a suggestion
    is shown with the link control, and a near miss offered there invites the
    owner to link the wrong repository. Forks and repositories already linked
    are not offered one.
    """
    by_name = {_comparable(name): name for name in names}
    return {
        project.id: by_name[_comparable(project.name)]
        for project in projects
        if not project.is_fork
        and project.resume_entry is None
        and _comparable(project.name) in by_name
    }


def evidence_digest(linked: Mapping[str, str]) -> str:
    """A stable digest of `linked`, for the tailoring cache key. Empty when nothing is linked."""
    if not linked:
        return ""
    material = "\x1f".join(f"{key}\x1e{linked[key]}" for key in sorted(linked))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
