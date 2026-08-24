"""Tailor ahead of time, so the morning queue is already built.

Tailoring is the slowest thing in the apply pipeline and the only part that
spends a quota. Doing it inside `apply_job` means every application waits on a
model, and at fifty a day that is the difference between a queue that flows
and one that stalls on each card.

So it moves out of the critical path: run this overnight, and by morning every
posting worth applying to already has a rendered PDF attached to its `Match`.
The apply pipeline reuses it and never calls a model at all.

## What gets tailored, and why that gate matters

**Only postings the owner marked `interested`.** Not the whole match feed.
That is not a performance choice — it is what makes the quota arithmetic work.
The free tier this project targets cannot cover five bullets × every scored
posting, and it does not need to: the owner applies to the ones they picked.
Swiping is what turns "tailor everything" into "tailor thirty things".

## Stopping before the allowance is gone

A batch that spends the last of the day's calls halfway through leaves the
owner with a half-tailored queue and no way to tell which half. This reserves
a margin and stops cleanly while it can still report what it did, rather than
discovering the limit by hitting it.

## What it does not do

Retry a posting that failed. A provider error is recorded and the posting is
left untailored, which is a state the apply pipeline already handles by
sending the base résumé. Retrying inside a batch turns one bad night into a
quota spent entirely on failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Match, Posting, Profile, Project, Resume
from packages.github.select import select_projects
from packages.llm import quota
from packages.llm.provider import LLMProvider
from packages.tailor.evidence import matched_job_terms
from packages.tailor.guard import SourceCorpus
from packages.tailor.parse import ParsedResume
from packages.tailor.publish import publish_tailored
from packages.tailor.rewrite import tailor_bullets

log = structlog.get_logger(__name__)

#: Calls held back from the day's allowance. Enough for the owner to tailor a
#: couple of things by hand afterwards, and enough that the batch stops while
#: it can still report rather than while raising.
QUOTA_MARGIN = 20

#: Default ceiling on one run. A batch is meant to finish overnight, and an
#: unbounded one across a large feed will not.
DEFAULT_LIMIT = 50


@dataclass
class BatchResult:
    tailored: int = 0
    skipped_existing: int = 0
    failed: int = 0
    #: Set when the run stopped early rather than finishing its list.
    stopped_reason: str | None = None
    calls_spent: int = 0
    per_posting: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        base = (
            f"{self.tailored} tailored, {self.skipped_existing} already done, "
            f"{self.failed} failed, ~{self.calls_spent} calls"
        )
        return f"{base} — stopped: {self.stopped_reason}" if self.stopped_reason else base


async def pending(
    session: AsyncSession,
    profile_id: str | None = None,
    *,
    limit: int = DEFAULT_LIMIT,
) -> list[tuple[Match, Posting]]:
    """Matches the owner kept that have no tailored résumé yet.

    Highest score first, so a run cut short by the quota has spent it on the
    strongest matches rather than on whatever the database returned first.
    """
    stmt = (
        select(Match, Posting)
        .join(Posting, Posting.id == Match.posting_id)
        .where(
            Match.decision == "interested",
            Match.tailored_resume_id.is_(None),
            Posting.closed_at.is_(None),
            Posting.description_raw.is_not(None),
        )
        .order_by(Match.score.desc())
        .limit(limit)
    )
    if profile_id is not None:
        stmt = stmt.where(Match.profile_id == profile_id)
    return [(match, posting) for match, posting in (await session.execute(stmt)).all()]


async def run(
    session: AsyncSession,
    provider: LLMProvider,
    *,
    profile_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> BatchResult:
    """Tailor everything pending, stopping before the allowance runs out."""
    result = BatchResult()
    work = await pending(session, profile_id, limit=limit)
    if not work:
        return result

    provider_name = getattr(provider, "name", "unknown")

    for match, posting in work:
        profile = await session.get(Profile, match.profile_id)
        if profile is None or profile.base_resume_id is None:
            result.failed += 1
            continue

        resume = await session.get(Resume, profile.base_resume_id)
        if resume is None or not resume.parsed_json:
            result.failed += 1
            continue

        parsed = ParsedResume.model_validate(resume.parsed_json)
        bullets = [line for line in parsed.section("experience") if line.strip()]
        if not bullets:
            result.failed += 1
            continue

        # Checked per posting rather than once: the count moves as the batch
        # spends it, and the point is to stop before the last call, not after.
        left = quota.remaining(provider_name)
        if left is not None and left <= QUOTA_MARGIN + len(bullets):
            result.stopped_reason = (
                f"{provider_name} has ~{left} calls left today; stopping with a margin "
                "rather than leaving the queue half-tailored"
            )
            break

        project_inventory = list(
            (
                await session.scalars(
                    select(Project).where(Project.candidate_id == profile.candidate_id)
                )
            ).all()
        )
        ranked_projects = select_projects(
            project_inventory,
            posting.description_raw or "",
        )
        # Exact GitHub evidence (or an explicit pin) is required here. A recent
        # but unrelated repository should not consume résumé space merely
        # because the inventory has fewer than four projects.
        relevant_projects = [
            project
            for project in ranked_projects
            if project.pinned or matched_job_terms(project, posting.description_raw or "")
        ]

        try:
            rewrites = await tailor_bullets(
                provider, bullets, posting.description_raw or "", SourceCorpus.from_resume(parsed)
            )
        except Exception as exc:  # noqa: BLE001 - one posting must not end the night
            log.warning("batch_tailor_failed", error=type(exc).__name__)
            result.failed += 1
            continue

        result.calls_spent += len(bullets)

        # Every rewrite refused means the output is the source résumé. Storing
        # it would spend a row and a render to attach a document identical to
        # the one already on the profile, and would make the apply pipeline
        # report a tailored résumé that is not tailored.
        if all(rewrite.used_fallback for rewrite in rewrites.bullets) and not relevant_projects:
            result.failed += 1
            result.per_posting.append((posting.title or str(posting.id), "every rewrite refused"))
            continue

        published = await publish_tailored(
            session,
            candidate_id=profile.candidate_id,
            parsed=parsed,
            result=rewrites,
            projects=relevant_projects,
        )
        if published is None:
            result.failed += 1
            continue

        match.tailored_resume_id = published.id
        result.tailored += 1
        project_label = (
            f", {len(relevant_projects)} GitHub "
            f"{'project' if len(relevant_projects) == 1 else 'projects'} added"
            if relevant_projects
            else ""
        )
        result.per_posting.append(
            (
                posting.title or str(posting.id),
                f"{rewrites.changed_count} bullets rewritten{project_label}",
            )
        )
        await session.flush()

    await session.commit()
    log.info("batch_tailor_complete", summary=result.summary())
    return result
