"""`make eval-retrieval` — where each benchmark question's own posting lands.

    python -m scripts.eval_retrieval
    python -m scripts.eval_retrieval --set heldout --json out.json

Runs the questions in `seeds/retrieval_questions.json` through the real
`retrieve()` against the configured database and prints one row for each set
and each form of question. Read-only, in one transaction, and no model is
asked anything. `packages/matching/known_target.py` says what the numbers are
and are not; `docs/RAG_PIPELINE.md` says why this exists.

The settings it runs under are the ones in force (`CHAT_SEMANTIC_CANDIDATES`,
`CHAT_RERANK_MODEL`), and the report says which. To compare two, run it twice.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import text

from packages.core.db import get_engine, get_sessionmaker
from packages.matching import known_target


def render(report: known_target.Report, tuned_on: set[str]) -> str:
    stamp = report.stamp
    lines = [
        f"measured {stamp['measured']} over {stamp['open_postings']} open postings",
        f"chunk vectors: {', '.join(stamp['chunk_models']) or 'none'} | "
        f"semantic candidates: {'on' if stamp['semantic_candidates'] else 'off'} | "
        f"second model: {stamp['rerank_model'] or 'none'} | "
        f"search area: {'applied' if stamp['search_area_applied'] else 'not applied'}",
        f"{report.cases} cases: {report.present} asked, {report.gone} whose posting has closed, "
        f"{report.outside_area} outside the search area",
        "",
        f"{'set':9}{'question':13}{'asked':>6}{'first':>7}{'in 5':>6}"
        f"{'listed':>8}{'MRR':>8}{'median s':>10}",
    ]
    for row in report.rows:
        lines.append(
            f"{row.set:9}{row.kind:13}{row.questions:>6}{row.first:>7}{row.in_five:>6}"
            f"{row.listed:>8}{row.mrr:>8.3f}{row.median_seconds:>10.2f}"
        )
    if not report.rows:
        lines.append("no question's posting is open in this database")
    lines += [
        "",
        "One target a question: a paraphrase that finds five other postings of the same kind",
        "scores as a miss. This is where a known posting lands, not whether answers are relevant.",
    ]
    if tuned_on:
        lines.append(
            f"Search rules were chosen on {', '.join(sorted(tuned_on))}. Read the other set for "
            "whether a change holds."
        )
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=Path, default=known_target.DEFAULT_SEED)
    parser.add_argument("--set", dest="only", help="one set by name, such as heldout")
    parser.add_argument("--json", type=Path, help="also write the full report here")
    args = parser.parse_args()

    cases = [c for c in known_target.load(args.seed) if args.only in (None, c.set)]
    try:
        async with get_sessionmaker()() as session:
            # One snapshot for every question, and a transaction that cannot write.
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            report = await known_target.run(session, cases)
            await session.rollback()
    finally:
        await get_engine().dispose()

    print(render(report, {c.set for c in cases if c.tuned_on}))
    if args.json is not None:
        args.json.write_text(json.dumps(asdict(report), indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
