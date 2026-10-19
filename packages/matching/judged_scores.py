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

`compare` pairs each candidate question with the shipped search's same
question. It resamples differences, not two independent means. Dev is the
default; the held-out half must be requested explicitly for the final decision.
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
#: How a grade Claude gave is marked, at the owner's request, so a baseline
#: can always say how many of its grades are the owner's own.
CLAUDE_NOTE = "claude:"


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


PAIRED_METRICS = ("reciprocal_rank", "ndcg_at_depth", "recall_at_5", "precision_at_5")


@dataclass
class Comparison:
    candidate: str
    split: str
    stratum: str | None
    paired: int
    unanswerable: int
    differences: dict[str, dict[str, float]]
    means: dict[str, float]
    intervals: dict[str, tuple[float, float]]
    abstention_lost: list[str]
    abstention_gained: list[str]
    routing_lost: list[str]
    routing_gained: list[str]
    shipped: Row
    candidate_summary: Row


def compare(
    scores: Sequence[QuestionScore],
    candidate: str,
    *,
    split: str = "dev",
    stratum: str | None = None,
    seed: int = 0,
) -> Comparison:
    """Candidate minus shipped, paired by question, with a seeded 95% CI.

    Both modes must have exactly the same questions and relevant set. A pair
    with no relevant postings is counted, not averaged. Latency covers every
    selected question; coverage covers answer questions, as in `summarise`.
    Nothing is rounded here: rounding an interval can hide whether it includes
    zero. The caller can format it for a table after making that decision.
    """
    if split not in {"dev", "held_out"}:
        raise ValueError("split must be dev or held_out")
    if candidate == "shipped":
        raise ValueError("candidate must differ from shipped")
    chosen = [
        s
        for s in scores
        if s.mode in {"shipped", candidate}
        and s.held_out == (split == "held_out")
        and (stratum is None or s.stratum == stratum)
    ]
    modes: dict[str, dict[str, QuestionScore]] = {"shipped": {}, candidate: {}}
    for score in chosen:
        if score.key in modes[score.mode]:
            raise ValueError(f"duplicate question {score.key!r} in {score.mode}")
        modes[score.mode][score.key] = score
    shipped, other = modes["shipped"], modes[candidate]
    if not shipped or shipped.keys() != other.keys():
        raise ValueError("both modes must contain the same nonempty question set")
    differences: dict[str, dict[str, float]] = {}
    unanswerable = 0
    flips: dict[str, list[str]] = {
        f"{kind}_{way}": [] for kind in ("abstention", "routing") for way in ("lost", "gained")
    }
    for key in sorted(shipped):
        left, right = shipped[key], other[key]
        metadata = ("stratum", "expect", "held_out", "relevant", "gone")
        if any(getattr(left, name) != getattr(right, name) for name in metadata):
            raise ValueError(f"incomparable metadata for question {key!r}")
        if left.expect != "answer":
            if left.correct is None or right.correct is None:
                raise ValueError(f"missing abstention verdict for question {key!r}")
            kind = "abstention" if left.expect == "none" else "routing"
            if left.correct and not right.correct:
                flips[f"{kind}_lost"].append(key)
            elif right.correct and not left.correct:
                flips[f"{kind}_gained"].append(key)
            continue
        left_values = [getattr(left, metric) for metric in PAIRED_METRICS]
        right_values = [getattr(right, metric) for metric in PAIRED_METRICS]
        if all(value is None for value in left_values + right_values):
            unanswerable += 1
            continue
        if any(value is None for value in left_values + right_values):
            raise ValueError(f"missing paired metric for question {key!r}")
        differences[key] = {
            metric: float(b) - float(a)
            for metric, a, b in zip(PAIRED_METRICS, left_values, right_values, strict=True)
        }
    means = {}
    intervals = {}
    for metric in PAIRED_METRICS:
        values = [delta[metric] for delta in differences.values()]
        if values:
            means[metric] = statistics.fmean(values)
            intervals[metric] = bootstrap_ci(
                values, lambda xs: statistics.fmean(cast(Sequence[float], xs)), seed=seed
            )
    summaries = {row.mode: row for row in summarise(chosen, split)}
    # Coverage and timing are thresholds too. Keep their full precision here,
    # even though the legacy display rounds them to three decimal places.
    for name, mine in modes.items():
        row = summaries[name]
        answers = [s for s in mine.values() if s.expect == "answer"]
        returned = sum(s.returned for s in answers)
        row.coverage = sum(s.judged for s in answers) / returned if returned else 1.0
        row.median_seconds = statistics.median(s.seconds for s in mine.values())
    return Comparison(
        candidate=candidate,
        split=split,
        stratum=stratum,
        paired=len(differences),
        unanswerable=unanswerable,
        differences=differences,
        means=means,
        intervals=intervals,
        abstention_lost=flips["abstention_lost"],
        abstention_gained=flips["abstention_gained"],
        routing_lost=flips["routing_lost"],
        routing_gained=flips["routing_gained"],
        shipped=summaries["shipped"],
        candidate_summary=summaries[candidate],
    )


__all__ = [
    "CLAUDE_NOTE",
    "Comparison",
    "METRICS",
    "PAIRED_METRICS",
    "RELEVANT",
    "SHOWN",
    "QuestionScore",
    "Row",
    "as_dict",
    "compare",
    "score",
    "score_ranking",
    "summarise",
]
