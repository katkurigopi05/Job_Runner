"""Display-only project coverage for a page of matches; never a ranking input."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Match, Posting, Profile, Project
from packages.core.models_skill_links import PostingSkill, PostingSkillRead, ProjectSkill
from packages.matching import skill_links
from packages.matching.graph_rag import is_eligible, project_keys
from packages.matching.skill_vocab import BY_KEY


@dataclass(frozen=True)
class ProjectCoverage:
    covered: list[str]
    missing: list[str]


async def for_page(
    session: AsyncSession, page: Sequence[tuple[Match, Posting]]
) -> dict[uuid.UUID, ProjectCoverage]:
    """Two batched reads, scoped to this page and each match profile's owner.

    Saved project links must still be declared by the current source facts.
    A missing/stale posting reading or no direct requirement yields no line.
    """
    if not page:
        return {}
    projects = await session.execute(
        select(Profile.id, Project, ProjectSkill.skill)
        .join(Project, Project.candidate_id == Profile.candidate_id)
        .join(ProjectSkill, ProjectSkill.project_id == Project.id)
        .where(Profile.id.in_({match.profile_id for match, _ in page}))
    )
    declared: dict[uuid.UUID, set[str]] = defaultdict(set)
    eligible_keys: dict[uuid.UUID, set[str]] = {}
    for profile_id, project, skill in projects:
        if project.id not in eligible_keys:
            eligible_keys[project.id] = (
                project_keys(project) if is_eligible(project, project.candidate_id) else set()
            )
        if skill in eligible_keys[project.id]:
            declared[profile_id].add(skill)
    requirements = await session.execute(
        select(PostingSkill.posting_id, PostingSkill.skill)
        .join(Posting, Posting.id == PostingSkill.posting_id)
        .join(PostingSkillRead, PostingSkillRead.posting_id == Posting.id)
        .where(
            Posting.id.in_({posting.id for _, posting in page}),
            skill_links.is_current(),
            skill_links.is_direct_requirement(),
        )
    )
    required: dict[uuid.UUID, set[str]] = defaultdict(set)
    for posting_id, skill in requirements:
        if skill in BY_KEY:
            required[posting_id].add(skill)
    return {
        match.id: ProjectCoverage(
            covered=sorted(
                BY_KEY[k].label for k in required[posting.id] & declared[match.profile_id]
            ),
            missing=sorted(
                BY_KEY[k].label for k in required[posting.id] - declared[match.profile_id]
            ),
        )
        for match, posting in page
        if required[posting.id]
    }
