"""Fit a topic model over the posting corpus and report what it found.

    make fit-topics              # 12 topics over every open posting
    make fit-topics k=20 n=500   # 20 topics, sampled down to 500 postings

Two jobs, and the second is the point:

1. **Read the topics.** Printed as their top terms. If they do not look like
    job families, the model is not worth building a signal on.
2. **Calibrate `MAX_TOPIC_ENTROPY`.** The threshold in `legitimacy.py` decides
    when a posting is "several roles at once", and picking that from theory is
    guessing. This prints the entropy distribution across the real corpus, so
    it can be set where the corpus actually separates.

**No model is persisted.** Fitting is a batch job and `legitimacy.assess()`
takes a model as an argument, so wiring this into discovery means storing a
fitted model somewhere and loading it per sweep — infrastructure that is not
built. Until it is, this is an analysis tool and `topic_focus` only appears
for a caller that fits a model itself. Recorded here rather than left to be
discovered.
"""

from __future__ import annotations

import argparse
import asyncio
import random

from sqlalchemy import select

from packages.core import db as core_db
from packages.core.models import Posting
from packages.matching.topics import DEFAULT_TOPICS, entropy, fit, top_terms


def _percentiles(values: list[float]) -> str:
    ordered = sorted(values)

    def at(fraction: float) -> float:
        if not ordered:
            return 0.0
        return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]

    return (
        f"p10={at(0.10):.2f}  p25={at(0.25):.2f}  p50={at(0.50):.2f}  "
        f"p75={at(0.75):.2f}  p90={at(0.90):.2f}  p99={at(0.99):.2f}"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topics", "-k", type=int, default=DEFAULT_TOPICS)
    parser.add_argument("--limit", "-n", type=int, default=0, help="sample down; 0 means all")
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    async with core_db.get_sessionmaker()() as session:
        postings = list(
            (
                await session.scalars(
                    select(Posting).where(
                        Posting.closed_at.is_(None), Posting.description_raw.isnot(None)
                    )
                )
            ).all()
        )

    if args.limit and len(postings) > args.limit:
        # Seeded so a run is repeatable; the whole point of the report is to
        # be compared against the next one.
        postings = random.Random(args.seed).sample(postings, args.limit)

    documents = [f"{p.title or ''}\n{p.description_raw or ''}" for p in postings]
    print(f"fitting {args.topics} topics over {len(documents)} postings...")

    model = fit(documents, topics=args.topics, iterations=args.iterations, seed=args.seed)

    print(f"\nvocabulary: {len(model.vocabulary)} terms\n")
    for topic in range(model.topics):
        print(f"  topic {topic:>2}: {' '.join(top_terms(model, topic, limit=10))}")

    spreads = [entropy(model.transform(document)) for document in documents]
    print(f"\nentropy across the corpus:\n  {_percentiles(spreads)}")
    print(
        "\nSet MAX_TOPIC_ENTROPY in packages/matching/legitimacy.py from the tail "
        "of that distribution, not from theory."
    )


if __name__ == "__main__":
    asyncio.run(main())
