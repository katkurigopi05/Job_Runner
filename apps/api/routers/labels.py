"""The labeling loop — `docs/BACKLOG.md` P1.

Every relevance label in this repo is a `FIXTURE`: a posting and a grade
written together, beside the code that reads them. `docs/ML_EVALUATION.md`
refuses to name a production ranking candidate because of it, and CLAUDE.md
§15 records that Gate 5 therefore does not answer the question it was written
to ask — whether the scorer works on *this owner's* material.

These routes are how that changes. The owner is served a real crawled posting
and grades it 0–3, and the grades export as `Provenance.OWNER`.

## Why this is not `/swipe` with more buttons

`packages/matching/feedback.py` names two weaknesses in swipe-derived labels.
One is that a swipe is binary. The other is that it is taken in feed order, so
only postings the ranker already surfaced are ever judged.

Adding a four-point scale to the existing feed fixes the first and leaves the
second in place — while stamping the result `owner`, the provenance a benchmark
trusts most. That would make the bias *less* visible than it is today, which is
the opposite of the point. `packages/matching/active.py` therefore draws from
all crawled postings, including ones with no `Match` row at all, and records
which stream offered each one so the finished corpus can be audited for the
bias rather than assumed clean.

Nothing here applies to anything, exactly as `/swipe` does not. A grade is a
statement about a posting, and §2.3 keeps submission behind explicit approval.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from packages.core.enums import ErrorCode
from packages.core.models import Match, Posting, PostingLabel, Profile
from packages.core.schemas import (
    LabelCandidateOut,
    LabelIn,
    LabelOut,
    LabelSummaryOut,
)
from packages.matching import active
from packages.matching.labels import RELEVANCE_SCALE
from packages.matching.owner_labels import MIN_USEFUL_LABELS, TARGET_LABELS

router = APIRouter(prefix="/labels", tags=["labels"])

#: How much posting body the grading screen needs. A grade is a judgement on
#: the role, not a close reading, and shipping 6kB of boilerplate per card
#: makes the loop slower without making it more accurate.
DESCRIPTION_CHARS = 1200


async def _resolve_profile(session: SessionDep, profile_id: uuid.UUID | None) -> Profile:
    """The profile to label for, defaulting to the only one when there is one.

    Defaulting is safe *here* because a grade is scoped to a profile and
    labeling the wrong one is recoverable by re-grading. It would not be safe
    on anything that reaches an employer.
    """
    if profile_id is not None:
        profile = await session.get(Profile, profile_id)
        if profile is None:
            raise ApiError(ErrorCode.NOT_FOUND, "profile not found")
        return profile

    profiles = (await session.scalars(select(Profile).limit(2))).all()
    if not profiles:
        raise ApiError(ErrorCode.NOT_FOUND, "no profile exists to label against")
    if len(profiles) > 1:
        raise ApiError(
            ErrorCode.INVALID_REQUEST,
            "more than one profile exists; pass profile_id to say which to label for",
        )
    return profiles[0]


@router.get("/next", response_model=list[LabelCandidateOut])
async def next_to_label(
    session: SessionDep,
    profile_id: uuid.UUID | None = None,
    size: int = Query(default=10, ge=1, le=50),
) -> list[LabelCandidateOut]:
    """The next postings to grade, mixed across the three streams.

    Already-graded postings are excluded, so the queue drains. Re-grading is
    still possible by posting the same `posting_id` again — a corpus you cannot
    correct is one you build carefully and slowly, which defeats the point.
    """
    profile = await _resolve_profile(session, profile_id)
    candidates = await active.next_batch(session, profile.id, size=size)

    return [
        LabelCandidateOut(
            posting_id=c.posting.id,
            title=c.posting.title,
            location=c.posting.location,
            url=c.posting.url,
            description=(c.posting.description_raw or "")[:DESCRIPTION_CHARS] or None,
            first_seen_at=c.posting.first_seen_at,
            stream=c.stream.value,
            score=c.score,
        )
        for c in candidates
    ]


@router.post("", response_model=LabelOut)
async def record_label(body: LabelIn, session: SessionDep) -> PostingLabel:
    """Record a grade. Re-grading the same posting overwrites.

    The ranker's current score is stored alongside rather than looked up later,
    because re-scoring moves it and "what did the ranker think at the moment a
    human disagreed with it" is the measurement. Recomputing answers a
    different question.
    """
    if body.relevance not in RELEVANCE_SCALE:
        raise ApiError(
            ErrorCode.INVALID_REQUEST,
            f"relevance must be one of {sorted(RELEVANCE_SCALE)}",
        )

    profile = await _resolve_profile(session, body.profile_id)
    posting = await session.get(Posting, body.posting_id)
    if posting is None:
        raise ApiError(ErrorCode.NOT_FOUND, "posting not found")

    score = await session.scalar(
        select(Match.score).where(Match.profile_id == profile.id, Match.posting_id == posting.id)
    )
    stream = await _stream_for(session, profile.id, score, body.served_stream)

    # An upsert rather than read-then-insert. Two writes racing both saw no
    # existing row, and the unique constraint then failed one of them — so a
    # double-tap on the grading screen (which key auto-repeat makes easy)
    # returned an error instead of recording a grade. The database decides,
    # once.
    #
    # `score_at_label` and `stream` are set on insert only. They describe what
    # the ranker thought when the posting was *first* graded, and a re-grade
    # after a re-score must not quietly restate that history.
    statement = (
        insert(PostingLabel)
        .values(
            profile_id=profile.id,
            posting_id=posting.id,
            relevance=body.relevance,
            note=body.note,
            score_at_label=float(score) if score is not None else None,
            stream=stream,
        )
        .on_conflict_do_update(
            constraint="uq_posting_labels_profile_posting",
            set_={
                "relevance": body.relevance,
                "note": body.note,
                "updated_at": datetime.now(UTC),
            },
        )
        .returning(PostingLabel)
    )
    label = (await session.scalars(statement)).one()
    await session.commit()
    await session.refresh(label)
    return label


async def _stream_for(
    session: SessionDep,
    profile_id: uuid.UUID,
    score: float | None,
    served_stream: str | None = None,
) -> str:
    """Which stream this posting came from.

    Recomputed rather than taken from the client. The stream is the audit trail
    for sampling bias, and a value the caller supplies is one a caller can get
    wrong — or, on a corpus someone wants to look clean, right on purpose.

    `served_stream` is the one concession, and it is deliberately one-way: it
    can only ever make the recorded stream *weaker*, never stronger. A client
    cannot use it to claim `unseen`; it can only withdraw the server's own
    claim to `unseen`. Forging it costs the forger accuracy and buys nothing.
    """
    # UNSEEN means one thing only: the ranker never scored this posting. It is
    # the single fact the bias check in `/labels/summary` reads, so a scored
    # posting must never land here however far from the midpoint it sits —
    # that would report a shortlist-only corpus as having escaped the
    # shortlist, which is the one wrong answer this column exists to prevent.
    if score is None:
        # ...and "no score now" is not "never scored". `score.score_and_store`
        # withdraws a stale `Match` row when a posting newly fails a hard
        # filter, so a card served as `uncertain` can arrive here with its row
        # already gone. Reproduced: served `uncertain`, stored `unseen`.
        #
        # `unseen` is therefore claimed only when a serve attests it. Silence
        # is not evidence: a caller that sends no hint — the MCP tools, curl, a
        # future client — has told us nothing, and defaulting that to the
        # strongest claim would put the hole straight back. `unknown` reads as
        # "not counted as unseen", which errs toward reporting more bias than
        # there is, the safe direction for a number whose whole job is to
        # certify the absence of bias.
        #
        # The cost is real and worth stating: a caller that grades without
        # passing the stream `/labels/next` gave it cannot contribute unseen
        # coverage. That is the honest outcome — it genuinely cannot say where
        # the posting came from.
        if served_stream == active.Stream.UNSEEN.value:
            return active.Stream.UNSEEN.value
        return active.Stream.UNKNOWN.value

    span = await active.score_range(session, profile_id)
    if span is None:
        # One scored posting, or every score identical. `_uncertain` needs a
        # span and returns nothing in that case, so `_confident` is what
        # actually served this. Calling it uncertain made the stored stream
        # disagree with the one the screen showed — and the stored one is the
        # audit trail, so it was the copy that lied.
        return active.Stream.CONFIDENT.value

    low, high = span
    # The top decile of the observed range is what `_confident` draws from.
    if score >= high - (high - low) * 0.1:
        return active.Stream.CONFIDENT.value
    # Everything else the ranker weighed in on. Wider than what `_uncertain`
    # actually serves, deliberately: the alternative is a fourth value meaning
    # "scored, but not served by any stream", which would split the audit
    # trail without telling anyone anything they could act on.
    return active.Stream.UNCERTAIN.value


@router.get("/summary", response_model=LabelSummaryOut)
async def summary(session: SessionDep, profile_id: uuid.UUID | None = None) -> LabelSummaryOut:
    """Where the corpus stands against what P1 asks for.

    Reports the stream mix beside the count, because they answer different
    questions. A hundred labels drawn entirely from the ranker's own shortlist
    is the failure this loop was built to avoid, and a total on its own cannot
    show it.
    """
    profile = await _resolve_profile(session, profile_id)

    by_grade_rows = (
        await session.execute(
            select(PostingLabel.relevance, func.count())
            .where(PostingLabel.profile_id == profile.id)
            .group_by(PostingLabel.relevance)
        )
    ).all()
    by_stream_rows = (
        await session.execute(
            select(PostingLabel.stream, func.count())
            .where(PostingLabel.profile_id == profile.id)
            .group_by(PostingLabel.stream)
        )
    ).all()

    by_grade = {int(g): int(c) for g, c in by_grade_rows}
    by_stream = {str(s or "unknown"): int(c) for s, c in by_stream_rows}
    total = sum(by_grade.values())

    notes: list[str] = []
    if total < MIN_USEFUL_LABELS:
        notes.append(
            f"{total} of the {MIN_USEFUL_LABELS} a ranking metric needs before it means anything."
        )
    if len([g for g, c in by_grade.items() if c]) < 2:
        notes.append(
            "One grade only — every metric is degenerate until the corpus "
            "disagrees with itself somewhere."
        )
    if total and not by_stream.get(active.Stream.UNSEEN.value):
        notes.append(
            "No labels from the unseen stream yet. Without them the corpus can "
            "only measure postings the ranker already surfaced, which is the "
            "bias these grades exist to escape."
        )

    return LabelSummaryOut(
        profile_id=profile.id,
        profile=profile.label,
        total=total,
        by_grade=by_grade,
        by_stream=by_stream,
        target=TARGET_LABELS,
        remaining=max(TARGET_LABELS - total, 0),
        # Stream coverage is part of the predicate, not merely a note beside
        # it. A hundred labels with two grades and no `unseen` is exactly the
        # corpus this loop was built to avoid, and calling that usable would
        # send the owner to `make export-labels kind=owner` for a set carrying
        # the bias `provenance: owner` is supposed to mean it escaped.
        usable=(
            total >= MIN_USEFUL_LABELS
            and len([g for g, c in by_grade.items() if c]) >= 2
            and bool(by_stream.get(active.Stream.UNSEEN.value))
        ),
        notes=notes,
    )
