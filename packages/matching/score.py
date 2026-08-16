"""Score postings against a profile, and persist the results as Matches.

Score is cosine similarity between the posting text and the profile's own
material (résumé plus projects), *after* the hard filters have removed
anything disqualifying. A filtered-out posting scores 0.0 and records why —
it never gets a middling score that might sneak past a threshold.

`reasons_json` on every Match carries the breakdown, because a match feed you
cannot interrogate is one you end up ignoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Match, Posting, Profile, Project, Resume
from packages.matching.embed import Embedder, cosine, get_embedder, tokenize
from packages.matching.filters import apply_filters

log = structlog.get_logger(__name__)

#: Weight of title overlap. A title is short but highly indicative — "Staff
#: Backend Engineer" says more per word than a paragraph of boilerplate.
TITLE_WEIGHT = 0.35
BODY_WEIGHT = 0.65


@dataclass
class ScoredPosting:
    posting_id: str
    score: float
    title_similarity: float = 0.0
    body_similarity: float = 0.0
    excluded_by: list[str] = field(default_factory=list)

    @property
    def excluded(self) -> bool:
        return bool(self.excluded_by)

    def reasons(self) -> dict[str, object]:
        return {
            "title_similarity": round(self.title_similarity, 4),
            "body_similarity": round(self.body_similarity, 4),
            "excluded_by": self.excluded_by,
        }


async def profile_text(session: AsyncSession, profile: Profile) -> str:
    """Everything the owner has that describes them.

    Résumé plus projects — the same corpus the fabrication guard treats as
    source facts, for the same reason: both are verified material rather than
    anything a model produced.
    """
    parts: list[str] = []

    if profile.base_resume_id:
        resume = await session.get(Resume, profile.base_resume_id)
        if resume and resume.parsed_json:
            lines = resume.parsed_json.get("raw_lines") or []
            parts.extend(str(line) for line in lines)

    projects = (
        await session.scalars(select(Project).where(Project.candidate_id == profile.candidate_id))
    ).all()
    for project in projects:
        parts.append(project.name)
        if project.description:
            parts.append(project.description)
        if project.language:
            parts.append(project.language)
        parts.extend(project.topics_json or [])

    for value in (profile.location, profile.work_auth):
        if value:
            parts.append(str(value))

    return "\n".join(parts)


def score_posting(
    posting: Posting,
    profile: Profile,
    profile_vector: list[float],
    embedder: Embedder,
    *,
    target_seniority: str | None = None,
) -> ScoredPosting:
    """Score one posting. Filtered-out postings score exactly 0.0."""
    verdict = apply_filters(profile, posting, target_seniority=target_seniority)
    if not verdict.passed:
        return ScoredPosting(posting_id=str(posting.id), score=0.0, excluded_by=verdict.reasons)

    title_vector = embedder.encode([posting.title or ""])[0]
    body_vector = embedder.encode([posting.description_raw or ""])[0]

    title_similarity = cosine(profile_vector, title_vector)
    body_similarity = cosine(profile_vector, body_vector)

    combined = TITLE_WEIGHT * title_similarity + BODY_WEIGHT * body_similarity

    return ScoredPosting(
        posting_id=str(posting.id),
        score=round(combined, 6),
        title_similarity=title_similarity,
        body_similarity=body_similarity,
    )


async def score_and_store(
    session: AsyncSession,
    profile: Profile,
    postings: list[Posting],
    *,
    embedder: Embedder | None = None,
    target_seniority: str | None = None,
    store_excluded: bool = False,
) -> list[ScoredPosting]:
    """Score postings for a profile and upsert Match rows. Does not commit.

    Excluded postings are not stored by default — the feed is for things worth
    looking at, and a table of zeros is noise. Pass `store_excluded=True` when
    debugging why something never appeared.
    """
    active = embedder or get_embedder()
    profile_vector = active.encode([await profile_text(session, profile)])[0]

    existing = {
        str(match.posting_id): match
        for match in (
            await session.scalars(select(Match).where(Match.profile_id == profile.id))
        ).all()
    }

    scored: list[ScoredPosting] = []
    for posting in postings:
        result = score_posting(
            posting, profile, profile_vector, active, target_seniority=target_seniority
        )
        scored.append(result)

        if result.excluded and not store_excluded:
            continue

        match = existing.get(result.posting_id)
        if match is None:
            session.add(
                Match(
                    profile_id=profile.id,
                    posting_id=posting.id,
                    score=result.score,
                    reasons_json=result.reasons(),
                )
            )
        else:
            match.score = result.score
            match.reasons_json = result.reasons()

    await session.flush()
    scored.sort(key=lambda s: s.score, reverse=True)
    log.info(
        "scored_postings",
        profile_id=str(profile.id),
        total=len(postings),
        excluded=sum(1 for s in scored if s.excluded),
        embedder=active.name,
    )
    return scored


async def embed_postings(
    session: AsyncSession, postings: list[Posting], *, embedder: Embedder | None = None
) -> int:
    """Fill in `description_embedding` for postings that lack one."""
    active = embedder or get_embedder()
    pending = [p for p in postings if p.description_embedding is None and p.description_raw]
    if not pending:
        return 0

    vectors = active.encode([f"{p.title or ''}\n{p.description_raw or ''}" for p in pending])
    for posting, vector in zip(pending, vectors, strict=True):
        posting.description_embedding = vector

    await session.flush()
    return len(pending)


def keyword_overlap(profile_text_value: str, posting: Posting) -> list[str]:
    """Terms shared by profile and posting — the human-readable 'why'."""
    profile_tokens = set(tokenize(profile_text_value))
    posting_tokens = tokenize(f"{posting.title or ''} {posting.description_raw or ''}")
    seen: list[str] = []
    for token in posting_tokens:
        if token in profile_tokens and token not in seen:
            seen.append(token)
    return seen[:20]
