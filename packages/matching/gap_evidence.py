"""What stands behind a recurring skill gap's count: its jobs, their quotes, its projects.

`graph_rag.graph_result` counts a gap as the distinct jobs, among the ones the
answer reviewed, that directly require the skill. This module turns that same
set into the drill-down the dock opens under Evidence details: every job's id,
the first few with the line each job requires the skill in, and the eligible
projects whose repositories declare it. It reads nothing and filters nothing.
The population and the projects arrive already decided by retrieval, so the
count and the list cannot drift apart.

Local rendering only. None of it enters a prompt: a remote model is shown skill
labels, anonymous project references and public posting lines
(`GraphEvidence.model_text`), never this.

**Declarations, not implementation.** A project's primary language and its
dependency files say the repository uses a technology. Resume Studio's
implementation findings (a file and lines at a commit) are made inside one
draft's live check and are not kept, so chat never has one; `GapProjectOut`
refuses any category but Studio's "declared".
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from packages.core.models import Posting, Project
from packages.core.schemas_graph import (
    GAP_JOBS_INLINE,
    GapEvidenceOut,
    GapJobOut,
    GapProjectOut,
)
from packages.github.code_signals import Classification
from packages.matching.skill_vocab import normalize_skill

#: `github_tools.live_projects_for_graph` pins a checked repository's URL to the
#: commit it read: `<repository>/tree/<sha>`.
_PINNED = re.compile(r"/tree/([0-9a-f]{40}|[0-9a-f]{64})$")

Declared = Literal["dependency_file", "primary_language"]


def declared_in(project: Project, skill: str) -> list[Declared]:
    """Where the repository declares `skill`: the same two facts `graph_rag.project_keys` reads."""
    where: list[Declared] = []
    if project.language and normalize_skill(project.language) == skill:
        where.append("primary_language")
    if any(normalize_skill(name) == skill for name in project.dependency_technologies or []):
        where.append("dependency_file")
    return where


def commit_of(url: str) -> str | None:
    """The commit a live check read the repository at, or None for imported metadata."""
    pinned = _PINNED.search(url)
    return pinned.group(1) if pinned else None


def gap_evidence(
    skill: str,
    label: str,
    source_label: str,
    contributors: Sequence[tuple[Posting, str | None, str]],
    *,
    reviewed: int,
    projects: Sequence[Project],
) -> GapEvidenceOut:
    """The drill-down for one gap.

    `contributors` is every job counted, each once, in the order the population
    was read, with its company and the line it requires the skill in.
    `projects` are the eligible projects the graph connected to the skill.
    """
    evidence = [
        GapProjectOut(
            project_id=project.id,
            repository=project.full_name or project.name,
            url=project.url,
            category=Classification.DECLARED,
            declared_in=where,
            commit_sha=commit_of(project.url),
        )
        for project in projects
        if (where := declared_in(project, skill))
    ]
    return GapEvidenceOut(
        skill=skill,
        label=label,
        source_label=source_label,
        total=len(contributors),
        reviewed=reviewed,
        posting_ids=[posting.id for posting, _, _ in contributors],
        jobs=[
            GapJobOut(
                posting_id=posting.id,
                title=posting.title or "(untitled)",
                company=company,
                location=posting.location,
                url=posting.url,
                quote=quote,
            )
            for posting, company, quote in contributors[:GAP_JOBS_INLINE]
        ],
        projects=evidence,
    )
