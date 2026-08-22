"""Re-score every open posting against the profiles as they stand now.

Scoring already happens, but only as a side effect: `crawl_job` and
`discover_job` re-score after a sweep, and both return early when the sweep
emitted nothing. So the one moment you most want a re-score — you just
replaced the résumé the score is computed *from* — is the one moment nothing
triggers it, and the feed keeps ranking against the résumé you replaced.

Nothing here is new arithmetic. It calls the same `score_and_store` the
crawler does, against the same open postings, and exists so that the trigger
does not have to be a crawl that happened to find something.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Match, Posting, Profile
from packages.matching.embed import Embedder
from packages.matching.score import embed_postings, score_and_store

log = structlog.get_logger(__name__)


@dataclass
class ProfileRescore:
    """What changed for one profile."""

    label: str
    profile_id: str
    #: Postings evaluated, which is not the same as rows written.
    scored: int = 0
    #: Failed a hard filter, so scored but deliberately not stored.
    excluded: int = 0
    #: Matches whose score moved by more than `MOVED_THRESHOLD`. The count that
    #: answers "did replacing the résumé actually do anything".
    moved: int = 0
    created: int = 0
    before_top: list[tuple[float, str]] = field(default_factory=list)
    after_top: list[tuple[float, str]] = field(default_factory=list)


@dataclass
class RescoreReport:
    postings: int = 0
    embedded: int = 0
    profiles: list[ProfileRescore] = field(default_factory=list)

    def summary(self) -> str:
        head = f"{self.postings} open postings"
        if self.embedded:
            head += f", {self.embedded} newly embedded"
        lines = [head]
        for entry in self.profiles:
            kept = entry.scored - entry.excluded
            lines.append(
                f"  {entry.label}: {entry.scored} scored, {kept} kept "
                f"({entry.excluded} excluded by a hard filter), "
                f"{entry.created} new, {entry.moved} moved"
            )
        return "\n".join(lines)


#: Below this a score has not meaningfully moved — it is float noise from a
#: re-encode, not a different answer.
MOVED_THRESHOLD = 0.001

TOP_N = 10


async def _top(session: AsyncSession, profile: Profile) -> list[tuple[float, str]]:
    rows = (
        await session.execute(
            select(Match.score, Posting.title)
            .join(Posting, Posting.id == Match.posting_id)
            .where(Match.profile_id == profile.id)
            .order_by(Match.score.desc())
            .limit(TOP_N)
        )
    ).all()
    return [(float(score), title or "") for score, title in rows]


async def rescore(
    session: AsyncSession,
    *,
    label: str | None = None,
    embedder: Embedder | None = None,
    re_embed: bool = False,
) -> RescoreReport:
    """Re-score open postings for every profile, or just the one named.

    Does not commit — the caller decides, which is what makes a dry run
    possible without a second code path.

    `re_embed=True` recomputes every posting vector instead of reusing the
    stored one. Needed after an `EMBEDDING_BACKEND` change, and pointless
    otherwise: it is the slow half of this function.
    """
    postings = list(
        (await session.scalars(select(Posting).where(Posting.closed_at.is_(None)))).all()
    )
    report = RescoreReport(postings=len(postings))
    if not postings:
        return report

    report.embedded = await embed_postings(session, postings, embedder=embedder, force=re_embed)

    stmt = select(Profile)
    if label is not None:
        stmt = stmt.where(Profile.label == label)
    profiles = list((await session.scalars(stmt)).all())

    for profile in profiles:
        entry = ProfileRescore(label=profile.label, profile_id=str(profile.id))
        entry.before_top = await _top(session, profile)

        before = {
            str(posting_id): float(score)
            for posting_id, score in (
                await session.execute(
                    select(Match.posting_id, Match.score).where(Match.profile_id == profile.id)
                )
            ).all()
        }

        scored = await score_and_store(session, profile, postings, embedder=embedder)
        entry.scored = len(scored)
        for result in scored:
            # An excluded posting is scored but not stored — counting it as a
            # created Match reports 9,069 new rows on a run that wrote none.
            if result.excluded:
                entry.excluded += 1
                continue
            previous = before.get(result.posting_id)
            if previous is None:
                entry.created += 1
            elif abs(previous - result.score) > MOVED_THRESHOLD:
                entry.moved += 1

        entry.after_top = await _top(session, profile)
        report.profiles.append(entry)
        log.info(
            "profile_rescored",
            profile=profile.label,
            scored=entry.scored,
            created=entry.created,
            moved=entry.moved,
        )

    return report
