"""Grading the assistant's search — Phase 5 of `docs/HARNESS_AUDIT_2026-10-15.md`.

`/labels` grades a posting against a profile: is this a job for me. These
routes grade what the chat search returned for a frozen question: is this
posting what was asked for. The pool is built by `make judge-pool-write`
(`packages/matching/judged.py`); these only serve it and record grades.

Served one question at a time, its postings in a stable shuffle, the dev
questions before the held-out ones, so a partly graded set can already be
worked on without touching what is held out. Which mode found a posting is
never served. Nothing here searches, and nothing here applies to anything.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from packages.core.enums import ErrorCode
from packages.core.models import Company, Posting
from packages.core.models_judgments import RetrievalJudgment
from packages.core.schemas_judgments import (
    JudgmentCandidateOut,
    JudgmentIn,
    JudgmentOut,
    JudgmentSplitOut,
    JudgmentSummaryOut,
)

router = APIRouter(prefix="/judgments", tags=["judgments"])

#: Enough of a posting to see whether it is what was asked. A skill is often
#: named in the requirements, near the end, so this is longer than `/labels`'.
DESCRIPTION_CHARS = 8000


async def _set_version(session: SessionDep, asked: str | None) -> str | None:
    """The set asked for, or the one pooled most recently."""
    if asked:
        return asked
    newest = await session.scalar(
        select(RetrievalJudgment.set_version).order_by(RetrievalJudgment.created_at.desc()).limit(1)
    )
    return str(newest) if newest is not None else None


@router.get("/next", response_model=list[JudgmentCandidateOut])
async def next_to_judge(
    session: SessionDep,
    set_version: str | None = None,
    size: int = Query(default=10, ge=1, le=50),
) -> list[JudgmentCandidateOut]:
    """The next ungraded postings, a question's together, dev questions first."""
    version = await _set_version(session, set_version)
    if version is None:
        return []
    in_set = RetrievalJudgment.set_version == version
    rank = (
        func.row_number()
        .over(partition_by=RetrievalJudgment.question_key, order_by=RetrievalJudgment.pool_order)
        .label("position")
    )
    count = func.count().over(partition_by=RetrievalJudgment.question_key).label("of")
    placed = select(RetrievalJudgment.id, rank, count).where(in_set).subquery()
    rows = (
        await session.execute(
            select(RetrievalJudgment, Posting, Company.name, placed.c.position, placed.c.of)
            .join(placed, placed.c.id == RetrievalJudgment.id)
            .join(Posting, Posting.id == RetrievalJudgment.posting_id)
            .outerjoin(Company, Company.id == Posting.company_id)
            .where(in_set, RetrievalJudgment.relevance.is_(None))
            .order_by(
                RetrievalJudgment.held_out,
                RetrievalJudgment.question_key,
                RetrievalJudgment.pool_order,
            )
            .limit(size)
        )
    ).all()
    return [
        JudgmentCandidateOut(
            id=judgment.id,
            set_version=judgment.set_version,
            question_key=judgment.question_key,
            question=judgment.question,
            position=position,
            of=of,
            posting_id=posting.id,
            title=posting.title,
            company=company,
            location=posting.location,
            url=posting.url,
            description=(posting.description_raw or "")[:DESCRIPTION_CHARS] or None,
            changed=judgment.content_hash is not None
            and judgment.content_hash != posting.content_hash,
            closed=posting.closed_at is not None,
        )
        for judgment, posting, company, position, of in rows
    ]


@router.post("/{judgment_id}", response_model=JudgmentOut)
async def record_judgment(
    judgment_id: uuid.UUID, body: JudgmentIn, session: SessionDep
) -> JudgmentOut:
    """Record a grade. Grading again overwrites; the pool row stays."""
    judgment = await session.get(RetrievalJudgment, judgment_id, with_for_update=True)
    if judgment is None:
        raise ApiError(ErrorCode.NOT_FOUND, "judgment not found")
    judgment.relevance = body.relevance
    judgment.note = body.note
    judgment.graded_at = datetime.now(UTC)
    # Read before the commit expires the row.
    recorded = JudgmentOut(
        id=judgment.id,
        question_key=judgment.question_key,
        posting_id=judgment.posting_id,
        relevance=judgment.relevance,
        note=judgment.note,
        graded_at=judgment.graded_at,
    )
    await session.commit()
    return recorded


@router.get("/summary", response_model=JudgmentSummaryOut)
async def summary(session: SessionDep, set_version: str | None = None) -> JudgmentSummaryOut:
    version = await _set_version(session, set_version)
    if version is None:
        return JudgmentSummaryOut(
            set_version=None,
            notes=["Nothing pooled yet. Run `make judge-pool-write` to build what to grade."],
        )
    rows = (
        await session.execute(
            select(
                RetrievalJudgment.question_key,
                RetrievalJudgment.held_out,
                RetrievalJudgment.relevance,
            ).where(RetrievalJudgment.set_version == version)
        )
    ).all()
    out = JudgmentSummaryOut(set_version=version, total=len(rows))
    splits = {False: JudgmentSplitOut(), True: JudgmentSplitOut()}
    open_questions: set[str] = set()
    for key, held_out, relevance in rows:
        split = splits[bool(held_out)]
        split.total += 1
        if relevance is None:
            open_questions.add(key)
            continue
        split.graded += 1
        out.graded += 1
        out.by_grade[relevance] = out.by_grade.get(relevance, 0) + 1
    questions = {key for key, _, _ in rows}
    out.remaining = out.total - out.graded
    out.dev, out.held_out = splits[False], splits[True]
    out.questions = len(questions)
    out.questions_done = len(questions - open_questions)
    if out.total and out.graded == out.total:
        out.notes.append("Every posting is graded. Freeze the baseline: make score-judgments.")
    return out
