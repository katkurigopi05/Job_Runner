"""`make eval-paraphrased` — the 500 paraphrased questions through the real search.

    python -m scripts.eval_paraphrased
    python -m scripts.eval_paraphrased --mode shipped --mode qwen_semantic --json out.json
    python -m scripts.eval_paraphrased --limit 20

Each question in `docs/evaluations/paraphrased_questions_500.md` was written
from one open posting; this prints where that posting lands, overall, by
family and for the 15 trap wordings. Modes are the graded harness's
(`seeds/chat_judgment_questions.json`), switched with `judged.searching_as`,
so `qwen_semantic` here is the mode the owner's grades measured.

Read-only, in one snapshot. A Qwen mode embeds each question with the local
query model through Ollama, and the model is released when the run ends.
`packages/matching/paraphrased_set.py` says what the numbers are not.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import text

from packages.core.db import get_engine, get_sessionmaker
from packages.matching import chunk_search, judged
from packages.matching import paraphrased_set as P


def render(report: P.Report) -> str:
    stamp = report.stamp
    lines = [
        f"mode {report.mode} {stamp['switches'] or '(settings in force)'} | measured "
        f"{stamp['measured']} over {stamp['open_postings']:,} open postings",
        f"{report.cases} questions: {report.asked} asked, {len(report.gone)} whose posting has "
        f"closed, {len(report.outside_area)} the search area now leaves out",
        "",
        f"{'group':40}{'asked':>6}{'error':>6}{'first':>7}{'in 5':>6}{'in 20':>7}{'listed':>8}"
        f"{'MRR':>7}{'5+twin':>8}{'MRR+tw':>8}{'med s':>7}",
    ]
    for row in report.rows:
        lines.append(
            f"{row.group[:39]:40}{row.questions:>6}{row.errors:>6}{row.first:>7}{row.in_five:>6}"
            f"{row.in_twenty:>7}{row.listed:>8}{row.mrr:>7.3f}{row.in_five_twins:>8}"
            f"{row.mrr_twins:>8.3f}{row.median_seconds:>7.2f}"
        )
    lines += [
        "",
        "One target a question: five other postings that answer it as well score as a miss.",
        "'+twin' also accepts the same employer's near-identical posting.",
    ]
    return "\n".join(lines)


async def _run_mode(mode: judged.Mode, cases: list[P.Case]) -> P.Report:
    with judged.searching_as(mode):
        async with get_sessionmaker()() as session:
            # One snapshot for every question, and a transaction that cannot write.
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            report = await P.run(session, cases, mode=mode.name, switches=mode.settings)
            await session.rollback()
    return report


async def main() -> int:
    modes = {mode.name: mode for mode in judged.load().modes}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", dest="path", type=Path, default=P.DEFAULT_SET)
    parser.add_argument(
        "--mode",
        action="append",
        choices=sorted(modes),
        help="repeat for several (default shipped)",
    )
    parser.add_argument("--limit", type=int, default=0, help="the first N questions only (0 = all)")
    parser.add_argument("--no-traps", action="store_true", help="skip the 15 trap wordings")
    parser.add_argument("--json", type=Path, help="write every outcome and row here")
    args = parser.parse_args()

    cases, traps = P.load(args.path)
    if args.limit:
        cases = cases[: args.limit]
        traps = [t for t in traps if t.number <= args.limit]
    asked = cases + ([] if args.no_traps else traps)

    reports = []
    try:
        for name in args.mode or ["shipped"]:
            report = await _run_mode(modes[name], asked)
            reports.append(report)
            print(render(report), end="\n\n", flush=True)
            if args.json is not None:  # after each mode, so a later failure keeps it
                args.json.write_text(
                    json.dumps([asdict(r) for r in reports], indent=1), encoding="utf-8"
                )
    finally:
        if any(modes[name].settings.get("chat_qwen_search") for name in args.mode or []):
            await chunk_search.release_embedder()
        await get_engine().dispose()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
