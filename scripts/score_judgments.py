"""`make score-judgments` — the assistant's search against the owner's grades.

    python -m scripts.score_judgments                 # dev questions, once all are graded
    python -m scripts.score_judgments --provisional   # while grading is still going on
    python -m scripts.score_judgments --freeze        # write the baseline file

Asks every frozen question again in every mode (`packages/matching/judged.py`)
and scores the answers against `/label/questions` grades
(`judged_scores.py` says what the numbers mean). Read-only, in one snapshot.

The held-out questions are scored but not printed. They are read once, when a
change to the search is decided (part 3); printing them on every run is how a
held-out set stops being one. `--freeze` writes both halves to
`storage/evaluation/` with what they were measured on: the set's hash, the
grades' hash, the code revision and the corpus.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select, text

from packages.core.config import get_settings
from packages.core.db import get_engine, get_sessionmaker
from packages.core.models import Posting
from packages.core.models_chunks import PostingChunk
from packages.core.models_judgments import RetrievalJudgment
from packages.matching import judged, judged_scores

#: Proposed, not approved. The owner approves them before any candidate is
#: run against the held-out half (docs/harness-audit/phase-5-retrieval.md).
PROPOSED_THRESHOLDS = {
    "held_out_mrr": "no lower than the baseline's",
    "held_out_ndcg_at_depth": "no lower than the baseline's",
    "better": "a paired 95% interval of the held-out difference that excludes zero",
    "abstention": "no question expecting nothing answered that the baseline got right",
    "median_seconds": "at most 1.0 s above the baseline's",
    "coverage": "at least 0.9 of a candidate's results graded, or grade more first",
}


def render(rows: list[judged_scores.Row], depth: int) -> str:
    header = (
        f"{'mode':15}{'n':>4}{'P@5':>16}{'R@5':>16}{f'R@{depth}':>16}{'MRR':>16}"
        f"{f'nDCG@{depth}':>16}{'judged':>8}{'none ok':>9}{'median s':>10}"
    )
    lines = [header]
    for row in rows:
        cells = "".join(
            f"{row.means[m]:>6.3f} [{row.intervals[m][0]:.2f},{row.intervals[m][1]:.2f}]"
            if m in row.means
            else f"{'-':>16}"
            for m in judged_scores.METRICS
        )
        lines.append(
            f"{row.mode:15}{row.answerable:>4}{cells}{row.coverage:>8.2f}"
            f"{f'{row.abstained_right}/{row.abstention_asked}':>9}{row.median_seconds:>10.2f}"
        )
    return "\n".join(lines)


def _revision() -> str:
    try:
        done = subprocess.run(  # noqa: S603 - a fixed command, no input
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", type=Path, default=judged.DEFAULT_SET)
    parser.add_argument("--provisional", action="store_true", help="score before every grade is in")
    parser.add_argument("--freeze", action="store_true", help="write the baseline file")
    args = parser.parse_args()

    questions = judged.load(args.set)
    try:
        async with get_sessionmaker()() as session:
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            in_set = RetrievalJudgment.set_version == questions.version
            total = await session.scalar(select(func.count()).where(in_set)) or 0
            graded = (
                await session.scalar(
                    select(func.count()).where(in_set, RetrievalJudgment.relevance.is_not(None))
                )
                or 0
            )
            if total == 0:
                print(
                    "Nothing pooled. Run `make judge-pool-write`, then grade on /label/questions."
                )
                return 2
            complete = graded == total
            if not complete and not args.provisional:
                print(
                    f"{graded} of {total} graded. A baseline needs every one: finish on "
                    "/label/questions, or pass --provisional for numbers that are not one."
                )
                return 2
            grades = (
                await session.execute(
                    select(
                        RetrievalJudgment.question_key,
                        RetrievalJudgment.posting_id,
                        RetrievalJudgment.relevance,
                    )
                    .where(in_set)
                    .order_by(RetrievalJudgment.question_key, RetrievalJudgment.posting_id)
                )
            ).all()
            corpus = {
                "open_postings": await session.scalar(
                    select(func.count()).select_from(Posting).where(Posting.closed_at.is_(None))
                ),
                "chunk_models": sorted(
                    (await session.scalars(select(PostingChunk.embedding_model).distinct())).all()
                ),
            }
            scores = await judged_scores.score(session, questions)
            await session.rollback()
    finally:
        await get_engine().dispose()

    kind = "baseline" if complete else "provisional"
    print(
        f"{questions.version}: {graded} of {total} graded, {kind}. Dev questions; "
        "held out scored and not shown."
    )
    print(render(judged_scores.summarise(scores, "dev"), questions.depth))
    if not args.freeze:
        return 0

    record = {
        "kind": kind,
        "set_version": questions.version,
        "set_sha256": hashlib.sha256(args.set.read_bytes()).hexdigest(),
        "grades_sha256": hashlib.sha256(
            json.dumps([[k, str(p), g] for k, p, g in grades]).encode()
        ).hexdigest(),
        "graded": graded,
        "pooled": total,
        "code_revision": _revision(),
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "corpus": corpus,
        "depth": questions.depth,
        "modes": {mode.name: mode.settings for mode in questions.modes},
        "rows": {
            split: [asdict(row) for row in judged_scores.summarise(scores, split)]
            for split in ("dev", "held_out", "all")
        },
        "questions": judged_scores.as_dict(scores),
        "proposed_thresholds": PROPOSED_THRESHOLDS,
        "thresholds_approved": False,
    }
    folder = Path(get_settings().storage_root) / "evaluation"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = folder / f"{questions.version}-{kind}-{stamp}.json"
    out.write_text(json.dumps(record, indent=1, default=str))
    print(f"\nwritten {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
