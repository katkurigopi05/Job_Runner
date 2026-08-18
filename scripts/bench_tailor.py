"""Compare models on résumé tailoring.

    python -m scripts.bench_tailor [model ...]

The Ollama default was inherited, not chosen: `llama3.1` is hardcoded in
`OllamaProvider.__init__`. This measures the models actually installed, on the
same bullets and the same prompt, so the default can be a decision.

What it reports per model, and why each matters:

- **kept** — rewrites the guard accepted. Higher is not automatically better; a
  model that returns the original unchanged scores perfectly here.
- **rejected** — rewrites the guard refused. High means the model keeps
  reaching for things the résumé does not support.
- **changed** — accepted rewrites that actually differ from the source. This is
  the number that says whether tailoring did anything.
- **shrunk** — accepted rewrites shorter than the original. Usually a model
  dropping detail rather than re-emphasising it, which reads as a worse bullet.

Cloud-hosted models are marked. §2.8 permits the tailoring upload, but only
audited, and a benchmark that hides which runs left the machine would be the
same lie the audit was just fixed to stop telling.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass

from packages.llm.audit import is_local
from packages.llm.provider import OllamaProvider
from packages.tailor.diff import summarize
from packages.tailor.guard import SourceCorpus
from packages.tailor.rewrite import tailor_bullets

RESUME = """Gopi Krishna
Senior Engineer, Example Corp
Built backend services in Python.
Worked on reliability for internal tools.
Migrated a reporting job from cron to a queue.
Skills: Python, PostgreSQL, FastAPI"""

BULLETS = [
    "Built backend services in Python.",
    "Worked on reliability for internal tools.",
    "Migrated a reporting job from cron to a queue.",
]

JOB = """Senior Backend Engineer, Payments.
You will own high-throughput payment services in Python, improve the
reliability of a critical path, and work with PostgreSQL at scale. Experience
with queues and asynchronous processing is important. Kubernetes and Kafka
preferred."""

DEFAULT_MODELS = ("llama3.1", "mistral:latest", "phi3:mini", "gemma:latest")


@dataclass
class Result:
    model: str
    local: bool
    kept: int
    rejected: int
    changed: int
    shrunk: int
    seconds: float
    samples: list[tuple[str, str]]


async def bench(model: str) -> Result:
    corpus = SourceCorpus.from_texts(RESUME)
    started = asyncio.get_event_loop().time()
    result = await tailor_bullets(OllamaProvider(model=model), BULLETS, JOB, corpus)
    elapsed = asyncio.get_event_loop().time() - started

    summary = summarize(result)
    shrunk = sum(
        1 for b in result.bullets if not b.used_fallback and len(b.tailored) < len(b.original) * 0.9
    )
    return Result(
        model=model,
        local=is_local("ollama", model),
        kept=sum(1 for b in result.bullets if not b.used_fallback),
        rejected=summary.rejected,
        changed=summary.changed,
        shrunk=shrunk,
        seconds=round(elapsed, 1),
        samples=[(b.original, b.tailored) for b in result.bullets if not b.used_fallback],
    )


async def main(models: tuple[str, ...]) -> int:
    print(f"{len(BULLETS)} bullets, one posting, prompt tailor.system\n")
    header = f"{'model':22} {'where':8} {'kept':>5} {'rej':>4} {'chg':>4} {'shrunk':>7} {'secs':>6}"
    print(header)
    print("-" * len(header))

    results: list[Result] = []
    for model in models:
        try:
            result = await bench(model)
        except Exception as exc:  # noqa: BLE001 - a missing model is a result
            print(f"{model:22} {'—':8} failed: {type(exc).__name__}")
            continue
        results.append(result)
        where = "local" if result.local else "CLOUD"
        print(
            f"{result.model:22} {where:8} {result.kept:>5} {result.rejected:>4} "
            f"{result.changed:>4} {result.shrunk:>7} {result.seconds:>6}"
        )

    for result in results:
        if not result.samples:
            continue
        print(f"\n--- {result.model} ---")
        for original, tailored in result.samples:
            if original.strip() != tailored.strip():
                print(f"  {original}\n    -> {tailored}")

    return 0


if __name__ == "__main__":
    chosen = tuple(sys.argv[1:]) or DEFAULT_MODELS
    raise SystemExit(asyncio.run(main(chosen)))
