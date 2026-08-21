"""`make eval-tailor` — measure tailoring quality against real postings.

    python -m scripts.eval_tailor                 # the configured provider
    python -m scripts.eval_tailor --model phi3:mini

Twelve postings crawled from live Greenhouse, Lever and Ashby boards, tailored
against a fixture résumé, and scored on what actually happened to the text.

Exits non-zero when a run is unhealthy, so it works as a gate rather than only
as a report. Unhealthy means one of: almost nothing changed, most rewrites
were refused, no supported term reached the output, or the output is markedly
shorter than the source. See `packages/tailor/evaluate.py` for why those four.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from packages.llm.provider import OllamaProvider, build_provider
from packages.tailor.evaluate import evaluate_golden
from packages.tailor.parse import Contact, ParsedResume

GOLDEN = Path("tests/fixtures/golden/postings.json")

#: A stand-in résumé. Deliberately generic and deliberately *not* the owner's:
#: a quality number should describe the tailorer, and swapping in a stronger
#: résumé would flatter it without anything improving.
FIXTURE_BULLETS = [
    "Built and maintained backend services in Python for a payments platform.",
    "Designed a Postgres schema handling 40 million rows with sub-100ms queries.",
    "Led migration from a monolith to six services, cutting deploy time to 9 minutes.",
    "Wrote the on-call runbook and ran the rotation for a team of eight.",
    "Added integration tests that took coverage from 41% to 87%.",
]


def fixture_resume() -> ParsedResume:
    return ParsedResume(
        contact=Contact(name="Fixture Owner", email="fixture@example.com"),
        sections={"experience": list(FIXTURE_BULLETS)},
        raw_lines=list(FIXTURE_BULLETS),
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=None, help="Ollama model to evaluate")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    if not GOLDEN.is_file():
        print(f"missing golden set: {GOLDEN}", file=sys.stderr)
        return 1

    postings = json.loads(GOLDEN.read_text(encoding="utf-8"))["postings"]
    provider = OllamaProvider(model=args.model) if args.model else build_provider()

    report = await evaluate_golden(provider, fixture_resume(), postings)

    if args.json:
        print(
            json.dumps(
                {
                    "summary": report.summary(),
                    "change_rate": round(report.change_rate, 3),
                    "rejection_rate": round(report.rejection_rate, 3),
                    "uptake_rate": round(report.uptake_rate, 3),
                    "unhealthy": [name for name, _ in report.unhealthy],
                },
                indent=2,
            )
        )
    else:
        print(f"\nprovider: {getattr(provider, 'name', '?')}\n")
        for label, q in report.per_posting:
            mark = "ok  " if q.healthy else "WARN"
            print(
                f"  [{mark}] {label[:58]:60} changed {q.change_rate:>4.0%}  "
                f"refused {q.rejection_rate:>4.0%}  uptake {q.uptake_rate:>4.0%}"
            )
        print(f"\n  {report.summary()}")
        for label, problems in report.unhealthy:
            print(f"\n  {label}")
            for problem in problems:
                print(f"    - {problem}")

    return 0 if not report.unhealthy else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
