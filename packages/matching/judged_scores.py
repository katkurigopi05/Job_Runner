"""Scoring the assistant's search against the owner's grades: the baseline.

Phase 5, part 2, of `docs/HARNESS_AUDIT_2026-10-15.md`. `judged.py` builds
the pool and the owner grades it on `/label/questions`. This asks every
question again in every mode and scores what comes back against those
grades, per mode and per split, with an interval on every mean.

What the numbers are, and are not:

- **Relevant is a grade of 1 or 2**, for precision, recall and MRR. nDCG uses
  the grade itself, with gain `2**grade - 1`.
- **A posting nobody graded counts as not relevant.** That is the usual
  pooled-judgment assumption, and it is only safe while the pool still covers
  what a mode returns, so judged coverage of each mode's results is reported
  beside every row. A new mode, or a crawl since the pool was built, lowers it.
- **A graded posting that has since closed is gone, not missed.** The search
  does not return closed postings, so it is taken out of the question's
  relevant set and counted.
- **A question with nothing relevant left is not averaged.** Recall over an
  empty set has no value, and scoring it 0 or 1 would move the mean for no
  reason. It is counted.
- **"None" and "not a search" questions are scored on what was said**: right
  when the mode returned nothing, or did not search.

It never says one mode is better. Comparing them, with the held-out half read
once, is part 3.
"""

from __future__ import annotations

import math
import statistics
import time
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Posting
from packages.core.models_judgments import RetrievalJudgment
from packages.matching import judged
from packages.matching.metrics import bootstrap_ci

#: What the model is shown of an answer.
SHOWN = 5
#: The lowest grade that counts as relevant.
RELEVANT = 1


@dataclass(frozen=True)
class QuestionScore:
    mode: str
    key: str
    stratum: str
    expect: str
    held_out: bool
    attempted: bool
    returned: int
    #: Of what was returned, to the set's depth, how many have a grade.
    judged: int
    relevant: int
    gone: int
    seconds: float
    precision_at_5: float | None = None
    recall_at_5: float | None = None
    recall_at_depth: float | None = None
    reciprocal_rank: float | None = None
    ndcg_at_depth: float | None = None
    #: For a question expecting nothing, or no search: was that what happened.
    correct: bool | None = None


def _dcg(gains: Sequence[int]) -> float:
    return sum((2.0**gain - 1.0) / math.log2(rank + 1) for rank, gain in enumerate(gains, start=1))


def score_ranking(
    ranked: Sequence[uuid.UUID], grades: dict[uuid.UUID, int], depth: int
) -> dict[str, float | None]:
    """Precision, recall, reciprocal rank and nDCG of one answer.

    `grades` holds only postings still open. Ungraded results count as 0.
    """
    top = list(ranked)[:depth]
    relevant = {posting for posting, grade in grades.items() if grade >= RELEVANT}
    if not relevant:
        return {
            "precision_at_5": None,
            "recall_at_5": None,
            "recall_at_depth": None,
            "reciprocal_rank": None,
            "ndcg_at_depth": None,
        }
    hits = [posting in relevant for posting in top]
    first = next((rank for rank, hit in enumerate(hits, start=1) if hit), None)
    ideal = _dcg(sorted(grades.values(), reverse=True)[:depth])
    return {
        # Over five, however many came back: an answer of two cannot score
        # 1.0 for having no room to be wrong.
        "precision_at_5": sum(hits[:SHOWN]) / SHOWN,
        "recall_at_5": sum(hits[:SHOWN]) / len(relevant),
        "recall_at_depth": sum(hits) / len(relevant),
        "reciprocal_rank": 1.0 / first if first else 0.0,
        "ndcg_at_depth": _dcg([grades.get(posting, 0) for posting in top]) / ideal,
    }


async def _grades(
    session: AsyncSession, version: str
) -> tuple[dict[str, dict[uuid.UUID, int | None]], set[uuid.UUID]]:
    """Each question's pooled postings and grades, and which of them are still open."""
    rows = (
        await session.execute(
            select(
                RetrievalJudgment.question_key,
                RetrievalJudgment.posting_id,
                RetrievalJudgment.relevance,
                Posting.closed_at,
            )
            .join(Posting, Posting.id == RetrievalJudgment.posting_id)
            .where(RetrievalJudgment.set_version == version)
        )
    ).all()
    pooled: dict[str, dict[uuid.UUID, int | None]] = {}
    still_open: set[uuid.UUID] = set()
    for key, posting_id, relevance, closed_at in rows:
        pooled.setdefault(key, {})[posting_id] = relevance
        if closed_at is None:
            still_open.add(posting_id)
    return pooled, still_open


async def score(session: AsyncSession, questions: judged.QuestionSet) -> list[QuestionScore]:
    """Every question in every mode, scored. Reads only."""
    pooled, still_open = await _grades(session, questions.version)
    scores: list[QuestionScore] = []
    for mode in questions.modes:
        with judged.searching_as(mode):
            for question in questions.questions:
                started = time.perf_counter()
                asked = await judged.ask(session, question.question, questions.depth)
                seconds = time.perf_counter() - started
                graded = pooled.get(question.key, {})
                open_grades = {
                    posting: grade
                    for posting, grade in graded.items()
                    if grade is not None and posting in still_open
                }
                gone = sum(
                    1
                    for posting, grade in graded.items()
                    if grade is not None and grade >= RELEVANT and posting not in still_open
                )
                metrics: dict[str, Any] = {}
                correct: bool | None = None
                if question.expect == "answer":
                    metrics = score_ranking(asked.ranked, open_grades, questions.depth)
                elif question.expect == "none":
                    correct = asked.attempted and not asked.ranked
                else:
                    correct = not asked.attempted
                scores.append(
                    QuestionScore(
                        mode=mode.name,
                        key=question.key,
                        stratum=question.stratum,
                        expect=question.expect,
                        held_out=question.held_out,
                        attempted=asked.attempted,
                        returned=len(asked.ranked),
                        judged=sum(
                            1 for posting in asked.ranked if graded.get(posting) is not None
                        ),
                        relevant=sum(1 for grade in open_grades.values() if grade >= RELEVANT),
                        gone=gone,
                        seconds=round(seconds, 3),
                        correct=correct,
                        **metrics,
                    )
                )
    return scores


METRICS = ("precision_at_5", "recall_at_5", "recall_at_depth", "reciprocal_rank", "ndcg_at_depth")


@dataclass
class Row:
    """One mode over one split: means with 95% intervals, and what they rest on."""

    mode: str
    split: str
    answerable: int
    unanswerable: int
    means: dict[str, float] = field(default_factory=dict)
    intervals: dict[str, tuple[float, float]] = field(default_factory=dict)
    #: Share of returned results, to depth, that carry a grade.
    coverage: float = 1.0
    abstained_right: int = 0
    abstention_asked: int = 0
    median_seconds: float = 0.0
    p90_seconds: float = 0.0


def summarise(scores: Sequence[QuestionScore], split: str) -> list[Row]:
    """`dev`, `held_out` or `all`, one row per mode."""
    chosen = [s for s in scores if split == "all" or (split == "held_out") == s.held_out]
    rows = []
    for mode in dict.fromkeys(s.mode for s in chosen):
        mine = [s for s in chosen if s.mode == mode]
        answerable = [s for s in mine if s.expect == "answer" and s.reciprocal_rank is not None]
        abstaining = [s for s in mine if s.expect != "answer"]
        row = Row(
            mode=mode,
            split=split,
            answerable=len(answerable),
            unanswerable=sum(1 for s in mine if s.expect == "answer") - len(answerable),
            abstained_right=sum(1 for s in abstaining if s.correct),
            abstention_asked=len(abstaining),
        )
        for metric in METRICS:
            values = [float(getattr(s, metric)) for s in answerable]
            if values:
                row.means[metric] = round(statistics.fmean(values), 3)
                low, high = bootstrap_ci(
                    values, lambda xs: statistics.fmean(cast(Sequence[float], xs))
                )
                row.intervals[metric] = (round(low, 3), round(high, 3))
        returned = sum(s.returned for s in mine if s.expect == "answer")
        if returned:
            row.coverage = round(sum(s.judged for s in mine if s.expect == "answer") / returned, 3)
        times = sorted(s.seconds for s in mine)
        if times:
            row.median_seconds = round(statistics.median(times), 3)
            row.p90_seconds = times[min(len(times) - 1, int(0.9 * len(times)))]
        rows.append(row)
    return rows


def as_dict(scores: Sequence[QuestionScore]) -> list[dict[str, Any]]:
    return [asdict(score) for score in scores]


__all__ = [
    "METRICS",
    "RELEVANT",
    "SHOWN",
    "QuestionScore",
    "Row",
    "as_dict",
    "score",
    "score_ranking",
    "summarise",
]
