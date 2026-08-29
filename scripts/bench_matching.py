"""Benchmark the ranking variants against a labeled set.

    python -m scripts.bench_matching
    python -m scripts.bench_matching --tag gate5 --json out.json
    python -m scripts.bench_matching --holdout 0.3 --seed 0

Prints one row per variant plus the verdict. The verdict is usually "not
established" — see `packages/matching/benchmark.py` for why that is the
harness working rather than failing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from packages.matching.benchmark import DEFAULT_K, default_variants, run_variant, summarize
from packages.matching.embed import LexicalEmbedder, get_embedder
from packages.matching.labels import load_labeled_set

DEFAULT_SET = Path("seeds/labeled_matches.yaml")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", type=Path, default=DEFAULT_SET, help="labeled set YAML")
    parser.add_argument("--tag", default="", help="only postings carrying this tag")
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    parser.add_argument(
        "--holdout",
        type=float,
        default=0.0,
        help="fraction held out, grouped by company. 0 runs on everything.",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--real-embedder",
        action="store_true",
        help="use EMBEDDING_BACKEND instead of the lexical default",
    )
    parser.add_argument("--json", type=Path, default=None, help="write records here")
    args = parser.parse_args(argv)

    dataset = load_labeled_set(args.set)
    items = dataset.tagged(args.tag) if args.tag else dataset.items
    if not items:
        print(f"no postings tagged {args.tag!r} in {args.set}", file=sys.stderr)
        return 2

    split = "all"
    if args.holdout:
        _, items = dataset.split(holdout=args.holdout, seed=args.seed)
        split = f"holdout({args.holdout}, seed={args.seed})"
        if args.tag:
            items = tuple(i for i in items if args.tag in i.tags)

    embedder = get_embedder() if args.real_embedder else LexicalEmbedder()
    records = [
        run_variant(v, dataset, items, k=args.k, split=split) for v in default_variants(embedder)
    ]
    verdict = summarize(records, k=args.k)

    print(f"dataset : {dataset.name} v{dataset.version} ({dataset.digest})")
    print(f"items   : {len(items)}  split={split}  labels={dataset.provenance_mix}")
    print(f"embedder: {type(embedder).__name__}")
    print()

    metric = f"ndcg@{args.k}"
    header = (
        f"{'variant':<22}{metric:>10}{'95% CI':>18}{'P@k':>8}{'MAP':>8}{'ROC':>8}{'ms/item':>9}"
    )
    print(header)
    print("-" * len(header))
    for record in verdict.records:
        m = record.metrics
        ci = f"[{m[f'{metric}_ci_low']:.2f},{m[f'{metric}_ci_high']:.2f}]"
        print(
            f"{record.variant:<22}{m[metric]:>10.3f}{ci:>18}"
            f"{m[f'precision@{args.k}']:>8.3f}{m['map']:>8.3f}"
            f"{m['roc_auc']:>8.3f}{record.latency_ms_per_item:>9.3f}"
        )

    print()
    print(f"leader             : {verdict.best}")
    if verdict.tied_with_best:
        print(f"statistically tied : {', '.join(verdict.tied_with_best)}")
    print(f"production candidate: {'YES' if verdict.production_candidate else 'NO'}")
    for blocker in verdict.blockers:
        print(f"  - {blocker}")

    leader = verdict.records[0]
    if leader.false_positives:
        print(f"\nirrelevant in top {args.k}: {', '.join(leader.false_positives)}")
    if leader.missed:
        print(f"relevant below {args.k} : {', '.join(leader.missed)}")

    if args.json:
        args.json.write_text(json.dumps([r.as_dict() for r in verdict.records], indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
