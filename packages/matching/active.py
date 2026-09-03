"""Choose which posting the owner should grade next.

`docs/BACKLOG.md` P1 asks for a labeling loop, and §59 for active learning:
serve the postings the scorer is least certain about first, so 100 labels buy
more than 100 random ones would.

## Two biases to avoid, not one

`packages/matching/feedback.py` records why swipe-derived labels are narrow.
One reason is that a swipe is binary; the other is that it is taken in feed
order, so **only postings the ranker already surfaced are ever labeled** and
the model ends up graded on its own shortlist.

A 0–3 scale served off the same feed fixes the first and leaves the second
untouched — while the labels now claim `provenance: owner`, the grade a
benchmark trusts most. So selection here draws from *all* crawled postings,
including ones with no `Match` row at all: filtered out by locality, by
seniority, or simply never scored.

That is also why uncertainty sampling alone is not the whole strategy. The
postings a scorer is least certain about are, by construction, ones it has an
opinion on. A corpus built only from those cannot show that the ranker is
confidently wrong about a whole category — which is the failure mode worth
finding.

## The three streams

`next_batch` interleaves, in this proportion:

- **Uncertain** — scored near the middle of the observed range. These are
  where a grade moves the decision boundary most.
- **Unseen** — crawled but never scored for this profile, or excluded by a
  hard filter. These are the only labels that can measure what the ranker
  buried, and nothing else produces them.
- **Confident** — a few from the top of the ranking. If the scorer's best
  picks are graded 0, no amount of boundary tuning helps, and that is worth
  learning early rather than after a hundred labels.

The mix is a constant here rather than a setting because a changed mix changes
what a label set can support, and that belongs in a commit message where
`ML_EVALUATION.md` can cite it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Match, Posting, PostingLabel

#: How many of each stream a batch of 10 carries. Uncertain leads because it
#: is the one with an information-theoretic argument behind it; unseen is
#: close behind because it is the only stream that can measure a miss.
STREAM_MIX: dict[str, int] = {"uncertain": 5, "unseen": 4, "confident": 1}


class Stream(StrEnum):
    """Why this posting was served. Recorded so a label set can be audited for
    the sampling bias it was built to avoid — a corpus that turned out to be
    all `uncertain` has the shortlist problem back."""

    UNCERTAIN = "uncertain"
    UNSEEN = "unseen"
    CONFIDENT = "confident"


@dataclass(frozen=True)
class Candidate:
    """One posting offered for grading."""

    posting: Posting
    stream: Stream
    #: The ranker's score, or None when it has no opinion — which is itself
    #: the fact worth grading against.
    score: float | None = None


def _labeled_subquery(profile_id: uuid.UUID) -> Select[tuple[uuid.UUID]]:
    """Postings this profile has already graded. Excluded from every stream."""
    return select(PostingLabel.posting_id).where(PostingLabel.profile_id == profile_id)


def _unlabeled(profile_id: uuid.UUID) -> Select[tuple[Posting]]:
    """Open postings this profile has not graded — the base of every stream."""
    return select(Posting).where(
        Posting.closed_at.is_(None),
        Posting.id.not_in(_labeled_subquery(profile_id)),
    )


async def score_range(session: AsyncSession, profile_id: uuid.UUID) -> tuple[float, float] | None:
    """The observed score span for this profile, or None when nothing is scored.

    Read rather than assumed. CLAUDE.md records that the shipped
    `min_match_score` of 0.75 was unreachable — the first real run over 10,922
    postings peaked at 0.271 — so a midpoint hardcoded at 0.5 would call
    nothing uncertain and this stream would silently return empty.
    """
    row = (
        await session.execute(
            select(func.min(Match.score), func.max(Match.score)).where(
                Match.profile_id == profile_id
            )
        )
    ).one()
    low, high = row
    if low is None or high is None or high <= low:
        return None
    return float(low), float(high)


async def _uncertain(session: AsyncSession, profile_id: uuid.UUID, limit: int) -> list[Candidate]:
    """Scored postings nearest the middle of the observed range."""
    span = await score_range(session, profile_id)
    if span is None or limit <= 0:
        return []
    midpoint = (span[0] + span[1]) / 2

    rows = (
        await session.execute(
            select(Posting, Match.score)
            .join(Match, and_(Match.posting_id == Posting.id, Match.profile_id == profile_id))
            .where(
                Posting.closed_at.is_(None),
                Posting.id.not_in(_labeled_subquery(profile_id)),
            )
            .order_by(func.abs(Match.score - midpoint))
            .limit(limit)
        )
    ).all()
    return [Candidate(posting=p, stream=Stream.UNCERTAIN, score=float(s)) for p, s in rows]


async def _unseen(session: AsyncSession, profile_id: uuid.UUID, limit: int) -> list[Candidate]:
    """Crawled postings this profile has no Match row for.

    The stream that exists because of the shortlist problem. A posting dropped
    by a hard filter has no Match row at all, so it can never be swiped, and
    without this it could never be labeled either — leaving the label set
    unable to say whether the filter was right.

    Newest first: a stale posting graded today teaches less, and `first_seen_at`
    is already indexed descending.
    """
    if limit <= 0:
        return []
    scored = select(Match.posting_id).where(Match.profile_id == profile_id)
    rows = (
        await session.scalars(
            _unlabeled(profile_id)
            .where(Posting.id.not_in(scored))
            .order_by(Posting.first_seen_at.desc())
            .limit(limit)
        )
    ).all()
    return [Candidate(posting=p, stream=Stream.UNSEEN, score=None) for p in rows]


async def _confident(session: AsyncSession, profile_id: uuid.UUID, limit: int) -> list[Candidate]:
    """The ranker's own top picks.

    Cheap insurance. If these grade 0, the problem is not the threshold and no
    amount of boundary sampling will show it.
    """
    if limit <= 0:
        return []
    rows = (
        await session.execute(
            select(Posting, Match.score)
            .join(Match, and_(Match.posting_id == Posting.id, Match.profile_id == profile_id))
            .where(
                Posting.closed_at.is_(None),
                Posting.id.not_in(_labeled_subquery(profile_id)),
            )
            .order_by(Match.score.desc())
            .limit(limit)
        )
    ).all()
    return [Candidate(posting=p, stream=Stream.CONFIDENT, score=float(s)) for p, s in rows]


def _quota(size: int) -> dict[str, int]:
    """Split a batch across the streams, summing to exactly `size`.

    Largest-remainder rather than rounding each share independently. Rounding
    over-allocated on every size that is not a multiple of the mix — `size=4`
    produced `2 + 2 + 1`, and the caller then truncated the tail, which silently
    dropped `confident` and then `unseen`. At `size=1` it produced `1 + 1 + 1`
    and truncation left uncertain alone: the shortlist bias this module exists
    to avoid, arriving through an arithmetic bug.

    Every stream gets at least one slot once the batch is large enough to hold
    them all. Below that a batch genuinely cannot carry the mix, and the shares
    decide who gets the slots — stated here rather than discovered later.
    """
    if size <= 0:
        return dict.fromkeys(STREAM_MIX, 0)

    total = sum(STREAM_MIX.values())
    if size >= len(STREAM_MIX):
        # One each, then distribute the rest by share.
        quota = dict.fromkeys(STREAM_MIX, 1)
        remaining = size - len(STREAM_MIX)
    else:
        quota = dict.fromkeys(STREAM_MIX, 0)
        remaining = size

    exact = {name: remaining * share / total for name, share in STREAM_MIX.items()}
    for name, value in exact.items():
        quota[name] += int(value)

    # Largest fractional remainder takes what integer division left behind.
    short = size - sum(quota.values())
    by_remainder = sorted(
        exact, key=lambda n: (exact[n] - int(exact[n]), STREAM_MIX[n]), reverse=True
    )
    for name in by_remainder[:short]:
        quota[name] += 1
    return quota


async def next_batch(
    session: AsyncSession, profile_id: uuid.UUID, size: int = 10
) -> list[Candidate]:
    """The next postings to grade, mixed across the three streams.

    A stream that comes up short does not steal from the others' quota — it
    just contributes less. Backfilling would quietly turn a batch into
    all-uncertain on a fresh database, which is the sampling bias this module
    exists to avoid, arriving through the back door.

    De-duplicated because the top of the ranking can also be near the midpoint
    on a corpus with a narrow score span, and grading the same posting twice in
    one sitting reads as a bug.
    """
    if size <= 0:
        return []
    quota = _quota(size)
    picked: list[Candidate] = []
    seen: set[uuid.UUID] = set()

    for candidates in (
        await _uncertain(session, profile_id, quota["uncertain"]),
        await _unseen(session, profile_id, quota["unseen"]),
        await _confident(session, profile_id, quota["confident"]),
    ):
        for candidate in candidates:
            if candidate.posting.id in seen:
                continue
            seen.add(candidate.posting.id)
            picked.append(candidate)

    return picked[:size]
