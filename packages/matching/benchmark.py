"""Compare ranking variants on a labeled set, and refuse to overclaim.

The master spec's §3 says not to assume any one model wins, §47 asks that every
experiment be recorded with the dataset it ran on, and §45 says an evaluator
must not be allowed to grade its own work. This is the harness those three
describe. It ranks nothing itself: it takes named variants, runs each over the
same labeled set, and reports metrics that can be compared.

## The control is the point

`CONSTANT` returns the same number for every posting. It is in the default
variant list and it is not a joke — under `metrics.TieBreak.PESSIMISTIC` it
scores what a scorer that has learned nothing should score, and any variant
that cannot clear it by more than the bootstrap interval has not been shown to
work. Most benchmark tables have no row like this, which is why so many of
them look impressive.

## Why a winner is usually not declared

`summarize()` produces `production_candidate: False` far more often than a
reader expects, for three reasons it names explicitly:

- The labeled set is fixture-only (`LabeledSet.is_fixture_only`). Then the
  number is a regression signal and nothing else — see CLAUDE.md §15.
- The set is too small for the difference to survive resampling. Thirty-two
  items gives an NDCG@10 interval roughly ±0.1 wide; two variants inside that
  band are tied, whatever their point estimates say.
- The margin over `CONSTANT` is inside its own interval.

Reporting "no winner" from a real benchmark is a result. Reporting a winner
from thirty-two synthetic labels would not be.

## What this does not do

It does not train anything. There is no supervised ranker here, and adding one
would need labels this repo does not have — 32 fixture-graded postings for one
profile cannot fit a learning-to-rank model without memorising them. The
pipeline, the metrics, and the holdout are the parts that can be built
honestly before that data exists; `docs/ML_EVALUATION.md` records exactly what
would have to arrive first.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from packages.core.models import Posting, Profile
from packages.matching.embed import Embedder, LexicalEmbedder, cosine
from packages.matching.labels import LabeledPosting, LabeledSet
from packages.matching.metrics import (
    Judgement,
    TieBreak,
    average_precision,
    bootstrap_ci,
    kendall_tau,
    ndcg_at_k,
    pr_auc,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    roc_auc,
    spearman,
)
from packages.matching.score import BODY_WEIGHT, TITLE_WEIGHT, score_posting

__all__ = [
    "ExperimentRecord",
    "Variant",
    "default_variants",
    "run_variant",
    "summarize",
]

#: Ranking cutoff for the reported metrics. Ten because that is roughly what
#: the owner reads before deciding the feed is not worth scrolling, and
#: because Gate 5 is already phrased in terms of a top ten.
DEFAULT_K = 10

#: A difference smaller than this is not reported as one even when the
#: intervals happen not to overlap. Two variants separated by 0.005 NDCG are
#: separated by nothing anyone will notice in a feed.
NEGLIGIBLE = 0.01

#: The benchmark profile reads as "Staff Engineer", which `detect_seniority`
#: maps to `senior`. Used only by the `production+seniority` variant.
BENCH_SENIORITY = "senior"


@dataclass(frozen=True)
class Variant:
    """One thing that can put postings in an order.

    `score` takes the profile text and a labeled posting and returns a number
    where higher means better. It receives no relevance label — a variant that
    could see the grade would be grading itself, which is §45's objection.
    """

    name: str
    description: str
    score: Callable[[str, LabeledPosting], float]
    #: Recorded in the experiment row. Free-form because a variant may be a
    #: cosine, a rule, or eventually a trained model.
    algorithm: str = ""
    embedding_model: str = ""
    hyperparameters: dict[str, object] = field(default_factory=dict)


@dataclass
class ExperimentRecord:
    """One variant's run over one dataset. The master spec's §47 row.

    Stored as a dataclass rather than a table because there is no experiment
    database yet and inventing one before there are experiments worth keeping
    would be the wrong order. `scripts/bench_matching.py` writes these to
    stdout and to JSON.
    """

    experiment_id: str
    variant: str
    algorithm: str
    embedding_model: str
    hyperparameters: dict[str, object]
    dataset_name: str
    dataset_version: str
    dataset_digest: str
    dataset_size: int
    provenance_mix: dict[str, int]
    fixture_only: bool
    split: str
    run_at: str
    metrics: dict[str, float]
    #: Milliseconds per scored posting. §48 lists latency as a selection
    #: criterion, and a cross-encoder that wins by 0.02 NDCG at fifty times
    #: the cost is not obviously the right choice for a nightly crawl.
    latency_ms_per_item: float
    #: Postings this variant put in the top k that the labels call irrelevant,
    #: and relevant ones it buried. Error analysis, §4.
    false_positives: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    notes: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "variant": self.variant,
            "algorithm": self.algorithm,
            "embedding_model": self.embedding_model,
            "hyperparameters": self.hyperparameters,
            "dataset": {
                "name": self.dataset_name,
                "version": self.dataset_version,
                "digest": self.dataset_digest,
                "size": self.dataset_size,
                "provenance": self.provenance_mix,
                "fixture_only": self.fixture_only,
                "split": self.split,
            },
            "run_at": self.run_at,
            "metrics": self.metrics,
            "latency_ms_per_item": self.latency_ms_per_item,
            "false_positives": self.false_positives,
            "missed": self.missed,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------


def _as_posting(item: LabeledPosting) -> Posting:
    """A detached Posting carrying the labeled fields.

    Not persisted and never added to a session. `score_posting` reads the ORM
    object without querying through it, so the production scorer can be
    benchmarked without a database — which is what keeps this runnable in CI.
    """
    return Posting(
        id=uuid.uuid5(uuid.NAMESPACE_URL, item.key),
        url=f"https://boards.greenhouse.io/bench/jobs/{item.key}",
        title=item.title,
        description_raw=item.description,
        location=item.location,
        ats_type="greenhouse",
        external_id=item.key,
    )


def _bench_profile() -> Profile:
    """The profile the filters see. Deliberately permissive.

    A benchmark of *ranking* should not be silently decided by a hard filter:
    if the location rule removes half the set, every variant scores the same
    and the comparison measures nothing. Filters are evaluated separately —
    `adj-junior-backend` is in the labeled set precisely so a seniority filter
    has something to be right about.

    `location` no longer does that work: the search-area filter reads the
    *posting*, not the profile (§1). What keeps the location rule out of the
    way now is that all 32 labeled postings say "Remote", which names a
    working mode and no place, so each classifies as `UNKNOWN` and is kept —
    32/32, measured. That holds only because a mode-only string reads as
    `UNKNOWN`; while it read as `UNPLACED` the area filter would have dropped
    the entire labeled set. The field is left as it is because the rest of
    `apply_filters` still takes a profile, and a labeled set that starts
    naming cities will need this re-checked rather than assumed.
    """
    return Profile(
        id=uuid.uuid5(uuid.NAMESPACE_DNS, "benchmark-profile"),
        candidate_id=uuid.uuid5(uuid.NAMESPACE_DNS, "benchmark-candidate"),
        label="benchmark",
        location="Remote",
        work_auth="US citizen",
        needs_sponsorship=False,
        links_json={},
        answers_kv_json={},
    )


def default_variants(
    embedder: Embedder | None = None,
    items: Sequence[LabeledPosting] | None = None,
) -> list[Variant]:
    """The shipped scorer, its ablations, and two controls.

    The ablations are here because §4 asks for them and because the weights in
    `score.py` (0.35 title, 0.65 body) have never been tested against any
    other split. `title_only` and `body_only` are the two ends of that dial;
    if either matches the combination, the weighting is doing no work.

    `items` adds the fusion variants. They are omitted rather than degraded
    when it is absent, because RRF reads the rank a signal assigned within a
    candidate set and there is no such thing as the rank of one posting on its
    own. A caller that cannot supply the corpus gets the per-item variants and
    an honestly shorter table.
    """
    active = embedder or LexicalEmbedder()

    def profile_vector(text: str) -> list[float]:
        return active.encode([text])[0]

    def production(text: str, item: LabeledPosting) -> float:
        return score_posting(
            _as_posting(item),
            _bench_profile(),
            profile_vector(text),
            active,
            profile_text_value=text,
        ).score

    def production_seniority(text: str, item: LabeledPosting) -> float:
        # The same scorer with the seniority filter armed. `score_posting`
        # takes `target_seniority=None` by default and `filters.seniority_ok`
        # passes everything when it is unset, so the shipped feed only filters
        # on level when a caller asks it to. This variant is what measures
        # whether asking is worth it.
        return score_posting(
            _as_posting(item),
            _bench_profile(),
            profile_vector(text),
            active,
            profile_text_value=text,
            target_seniority=BENCH_SENIORITY,
        ).score

    def title_only(text: str, item: LabeledPosting) -> float:
        return cosine(profile_vector(text), active.encode([item.title])[0])

    def body_only(text: str, item: LabeledPosting) -> float:
        return cosine(profile_vector(text), active.encode([item.description])[0])

    def jaccard(text: str, item: LabeledPosting) -> float:
        from packages.matching.embed import tokenize

        left = set(tokenize(text))
        right = set(tokenize(f"{item.title} {item.description}"))
        if not left or not right:
            return 0.0
        return len(left & right) / len(left | right)

    def constant(text: str, item: LabeledPosting) -> float:
        return 0.5

    # --- Fusion -----------------------------------------------------------
    #
    # Built once over the whole candidate set and then read per item, because
    # `Variant.score` is a per-item signature and a rank is not a per-item
    # fact. Computing it lazily on the first call would hide the corpus pass
    # inside one item's latency and make `ms/item` a lie about where the time
    # went, so it is done up front and the cost is spread evenly.
    fusion: list[Variant] = []
    if items:
        from packages.matching.hybrid import (
            BM25,
            BM25_B,
            BM25_K1,
            RRF_K,
            Ranking,
            agreement,
            fuse,
        )

        corpus = [(it.key, f"{it.title} {it.description}") for it in items]
        bm25 = BM25(corpus)

        def _rankings(text: str) -> tuple[Ranking, Ranking]:
            dense = Ranking.of(
                "dense",
                [
                    (it.key, cosine(profile_vector(text), active.encode([body])[0]))
                    for it, (_, body) in zip(items, corpus, strict=True)
                ],
            )
            return dense, Ranking.of("bm25", bm25.scores(text))

        def _fused(text: str) -> dict[str, float]:
            dense, lexical = _rankings(text)
            # Recorded on the variant rather than only computed, because the
            # fused metric is uninterpretable without it — see
            # `hybrid.agreement`. A reader who sees this variant lose needs to
            # know whether it lost as a fusion or was never one.
            fusion_agreement.append(agreement(dense, lexical))
            return fuse([dense, lexical], k=RRF_K)

        fusion_agreement: list[float] = []

        def _bm25_only(text: str) -> dict[str, float]:
            return dict(bm25.scores(text))

        # Memoized on the profile text: `run_variant` calls `score` once per
        # item with the same query, so without this the corpus is re-ranked
        # once per posting and the variant measures the cache miss.
        _cache: dict[tuple[str, str], dict[str, float]] = {}

        def _lookup(kind: str, text: str, item: LabeledPosting) -> float:
            key = (kind, text)
            if key not in _cache:
                _cache[key] = _fused(text) if kind == "rrf" else _bm25_only(text)
            return _cache[key].get(item.key, 0.0)

        def hybrid_rrf(text: str, item: LabeledPosting) -> float:
            return _lookup("rrf", text, item)

        def bm25_only(text: str, item: LabeledPosting) -> float:
            return _lookup("bm25", text, item)

        fusion = [
            Variant(
                name="hybrid_rrf",
                description="Reciprocal Rank Fusion of the dense cosine and BM25. "
                "Reads ranks, not scores, so no weight is fitted to these labels.",
                score=hybrid_rrf,
                algorithm="rrf(dense cosine, bm25)",
                embedding_model=type(active).__name__,
                hyperparameters={
                    "rrf_k": RRF_K,
                    "bm25_k1": BM25_K1,
                    "bm25_b": BM25_B,
                    # Filled by the first scoring pass. A list because the
                    # object is frozen by then; reading it later is the point.
                    "rank_agreement": fusion_agreement,
                },
            ),
            Variant(
                name="bm25_only",
                description="Ablation: the lexical half alone. If this matches the "
                "fusion, the dense side is contributing nothing.",
                score=bm25_only,
                algorithm="bm25",
                hyperparameters={"bm25_k1": BM25_K1, "bm25_b": BM25_B},
            ),
        ]

    return fusion + [
        Variant(
            name="production",
            description="What the feed ships today: score.py's weighted title/body cosine.",
            score=production,
            algorithm="weighted cosine + role-alias floor + hard filters",
            embedding_model=type(active).__name__,
            hyperparameters={"title_weight": TITLE_WEIGHT, "body_weight": BODY_WEIGHT},
        ),
        Variant(
            name="production+seniority",
            description="The shipped scorer with target_seniority armed — the opt-in "
            "half of filters.py that the feed does not use by default.",
            score=production_seniority,
            algorithm="weighted cosine + role-alias floor + hard filters (seniority armed)",
            embedding_model=type(active).__name__,
            hyperparameters={
                "title_weight": TITLE_WEIGHT,
                "body_weight": BODY_WEIGHT,
                "target_seniority": BENCH_SENIORITY,
            },
        ),
        Variant(
            name="title_only",
            description="Ablation: title cosine alone.",
            score=title_only,
            algorithm="cosine",
            embedding_model=type(active).__name__,
        ),
        Variant(
            name="body_only",
            description="Ablation: description cosine alone.",
            score=body_only,
            algorithm="cosine",
            embedding_model=type(active).__name__,
        ),
        Variant(
            name="jaccard",
            description="Rule baseline: token overlap over title and body, no embedding.",
            score=jaccard,
            algorithm="jaccard",
        ),
        Variant(
            name="constant",
            description="Control. Returns 0.5 for everything; anything that cannot beat it "
            "has not been shown to rank.",
            score=constant,
            algorithm="control",
        ),
    ]


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def run_variant(
    variant: Variant,
    dataset: LabeledSet,
    items: Sequence[LabeledPosting] | None = None,
    *,
    k: int = DEFAULT_K,
    split: str = "all",
    tie_break: TieBreak = TieBreak.PESSIMISTIC,
) -> ExperimentRecord:
    """Score every item with one variant and grade the ordering it produced."""
    subject = tuple(items if items is not None else dataset.items)
    if not subject:
        raise ValueError("nothing to score")

    started = time.perf_counter()
    judgements = [
        Judgement(
            score=variant.score(dataset.profile_text, item),
            relevance=item.relevance,
            key=item.key,
        )
        for item in subject
    ]
    elapsed_ms = (time.perf_counter() - started) * 1000

    scores = [j.score for j in judgements]
    labels = [1 if j.relevance > 0 else 0 for j in judgements]
    grades = [float(j.relevance) for j in judgements]

    ranked = sorted(judgements, key=lambda j: (-j.score, j.relevance))
    top = ranked[:k]

    # Both intervals, because their width is the honest statement about a set
    # this size. `optimistic` is not reported as the result — it is reported
    # so the gap between the two is visible.
    def ndcg(sample: Sequence[object]) -> float:
        return ndcg_at_k([j for j in sample if isinstance(j, Judgement)], k, tie_break=tie_break)

    low, high = bootstrap_ci(judgements, ndcg, samples=500, seed=17)

    metrics = {
        f"ndcg@{k}": ndcg_at_k(judgements, k, tie_break=tie_break),
        f"ndcg@{k}_ci_low": low,
        f"ndcg@{k}_ci_high": high,
        f"ndcg@{k}_optimistic": ndcg_at_k(judgements, k, tie_break=TieBreak.OPTIMISTIC),
        f"precision@{k}": precision_at_k(judgements, k, tie_break=tie_break),
        f"recall@{k}": recall_at_k(judgements, k, tie_break=tie_break),
        "map": average_precision(judgements, tie_break=tie_break),
        "mrr": reciprocal_rank(judgements, tie_break=tie_break),
        "roc_auc": roc_auc(scores, labels),
        "pr_auc": pr_auc(scores, labels),
        "spearman": spearman(scores, grades),
        "kendall_tau": kendall_tau(scores, grades),
    }

    return ExperimentRecord(
        experiment_id=f"{variant.name}-{dataset.digest}-{split}",
        variant=variant.name,
        algorithm=variant.algorithm,
        embedding_model=variant.embedding_model,
        hyperparameters=dict(variant.hyperparameters),
        dataset_name=dataset.name,
        dataset_version=dataset.version,
        dataset_digest=dataset.digest,
        dataset_size=len(subject),
        provenance_mix=dataset.provenance_mix,
        fixture_only=dataset.is_fixture_only,
        split=split,
        run_at=datetime.now(UTC).isoformat(timespec="seconds"),
        metrics={key: round(value, 6) for key, value in metrics.items()},
        latency_ms_per_item=round(elapsed_ms / len(subject), 4),
        false_positives=[j.key for j in top if j.relevance == 0],
        missed=[j.key for j in ranked[k:] if j.relevance > 0],
        notes=variant.description,
    )


@dataclass
class Comparison:
    """The verdict, and why it is usually 'not established'."""

    records: list[ExperimentRecord]
    best: str | None
    production_candidate: bool
    blockers: list[str] = field(default_factory=list)
    #: Variants whose intervals overlap the leader's. Reported as tied rather
    #: than ranked, because ordering them would imply a distinction the data
    #: does not carry.
    tied_with_best: list[str] = field(default_factory=list)


def summarize(records: Sequence[ExperimentRecord], *, k: int = DEFAULT_K) -> Comparison:
    """Rank the variants, then say what the ranking is not allowed to claim."""
    if not records:
        raise ValueError("no records to summarize")

    metric = f"ndcg@{k}"
    ordered = sorted(records, key=lambda r: -r.metrics.get(metric, 0.0))
    leader = ordered[0]

    blockers: list[str] = []
    if leader.fixture_only:
        blockers.append(
            "labels are fixture-only: the postings and their grades were written "
            "together, so this measures regression, not real-feed performance "
            "(CLAUDE.md §15)"
        )

    control = next((r for r in records if r.variant == "constant"), None)
    if control is not None and leader.variant != "constant":
        margin = leader.metrics.get(metric, 0.0) - control.metrics.get(metric, 0.0)
        interval = leader.metrics.get(f"{metric}_ci_high", 0.0) - leader.metrics.get(
            f"{metric}_ci_low", 0.0
        )
        if margin <= NEGLIGIBLE:
            blockers.append(f"leader beats the constant control by only {margin:.3f} {metric}")
        elif margin < interval / 2:
            blockers.append(
                f"margin over the control ({margin:.3f}) is inside the leader's own "
                f"bootstrap interval ({interval:.3f} wide): too small a set to separate them"
            )
    elif leader.variant == "constant":
        blockers.append("the constant control leads: no variant here is ranking at all")

    if leader.dataset_size < 100:
        blockers.append(
            f"{leader.dataset_size} labeled postings is below the ~100 needed for a "
            "difference of this size to survive resampling"
        )

    low = leader.metrics.get(f"{metric}_ci_low", 0.0)
    tied = [
        r.variant
        for r in ordered[1:]
        if r.metrics.get(f"{metric}_ci_high", 0.0) >= low
        or abs(r.metrics.get(metric, 0.0) - leader.metrics.get(metric, 0.0)) <= NEGLIGIBLE
    ]

    return Comparison(
        records=list(ordered),
        best=leader.variant,
        production_candidate=not blockers,
        blockers=blockers,
        tied_with_best=tied,
    )
