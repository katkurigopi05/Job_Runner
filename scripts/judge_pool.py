"""`make judge-pool` — the postings to grade for the assistant's search.

    python -m scripts.judge_pool            # what would be pooled; writes nothing
    python -m scripts.judge_pool --write    # add it to retrieval_judgments

Asks every question in `seeds/chat_judgment_questions.json` in every search
mode and pools what came back, to the set's depth, for grading on
`/label/questions`. No model is asked anything but the local re-ranker a
mode names. Writing only adds: a grade already given is never touched.
`packages/matching/judged.py` says why the pool is built this way.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import text

from packages.core.db import get_engine, get_sessionmaker
from packages.matching import judged


def render(questions: judged.QuestionSet, pool: judged.Pool) -> str:
    names = [mode.name for mode in questions.modes]
    lines = [
        f"set {questions.version}: {len(questions.questions)} questions, depth {questions.depth}, "
        f"modes {', '.join(names)}",
        "",
        f"{'question':26}{'stratum':14}{'held':>5}"
        + "".join(f"{name[:12]:>13}" for name in names)
        + f"{'to grade':>10}",
    ]
    wrong: list[str] = []
    for question in questions.questions:
        returned = [pool.asked[(question.key, name)] for name in names]
        cells = "".join(
            f"{'not searched' if not asked.attempted else len(asked.ranked):>13}"
            for asked in returned
        )
        lines.append(
            f"{question.key[:25]:26}{question.stratum[:13]:14}"
            f"{'yes' if question.held_out else '':>5}{cells}{pool.per_question(question.key):>10}"
        )
        for name, asked in zip(names, returned, strict=True):
            if question.expect == "none" and asked.ranked:
                count = len(asked.ranked)
                wrong.append(f"{question.key}: {name} returned {count} for a word no posting says")
            if question.expect == "not_searched" and asked.attempted:
                wrong.append(f"{question.key}: {name} searched a question that is not a search")
    held_out = {q.key for q in questions.questions if q.held_out}
    held = sum(1 for key, _ in pool.surfaced if key in held_out)
    lines += [
        "",
        f"to grade: {len(pool.surfaced)} ({len(pool.surfaced) - held} dev, {held} held out)",
    ]
    if wrong:
        lines += ["", "expected nothing, and got something (scored as wrong, not graded):"]
        lines += [f"  {line}" for line in wrong]
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", type=Path, default=judged.DEFAULT_SET)
    parser.add_argument("--write", action="store_true", help="add the pool to the database")
    args = parser.parse_args()

    questions = judged.load(args.set)
    try:
        async with get_sessionmaker()() as session:
            if not args.write:
                await session.execute(text("SET TRANSACTION READ ONLY"))
            pool = await judged.build_pool(session, questions)
            print(render(questions, pool))
            if args.write:
                added = await judged.write_pool(session, questions, pool)
                await session.commit()
                print(f"\nadded {added} to retrieval_judgments; grade them on /label/questions")
            else:
                await session.rollback()
                print("\nnothing written: run with --write (make judge-pool-write) to add it")
    finally:
        await get_engine().dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
