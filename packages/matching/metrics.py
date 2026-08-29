"""Ranking and classification metrics, computed honestly.

CLAUDE.md's Gate 5 asks that "the ones you'd actually apply rank in the top
10", and `tests/test_matching.py` checks exactly that: a count of wanted
postings in the top half. It is a real gate and it is nearly blind. It cannot
see the wanted posting that slid from rank 1 to rank 10, it cannot compare two
scorers that both pass, and it reports one bit where the question — is this
ranking better than that one — needs a number.

This module is that number. Nothing here scores a posting; it grades an
ordering that something else produced.

## Why no numpy

The metrics are a few dozen lines of arithmetic apiece and the repo has no
array dependency. Adding one so that `ndcg_at_k` can call a library function
would put torch-adjacent weight into `requirements` for code that a reader
should be able to check by hand against a worked example — which is what
`tests/test_matching_metrics.py` does.

## Ties are broken against the scorer, deliberately

This is the one design decision here with teeth. A scorer that returns the
same number for everything produces one tie group containing the whole feed,
and under the usual convention — stable sort, original order preserved — its
NDCG is whatever the input order happened to be. Feed it a list that starts
with the relevant items and a scorer that has learned nothing reports a
perfect ranking.

So a tie is resolved by putting the *less* relevant item first
(`TieBreak.PESSIMISTIC`, the default and the number that gets reported). A
scorer only earns credit for an ordering it actually produced. `OPTIMISTIC` is
available for the other end of the interval: when the two differ by much, the
scorer is not ranking, it is guessing, and the gap between them says so more
clearly than either number alone.

## Graded relevance

Relevance is an integer, not a bool. 0 is irrelevant and anything above it is
some degree of wanted; the binary case is the one where every label is 0 or 1.
Gain is `2**rel - 1`, so the distance from "would apply" to "dream job" is
worth more than the distance from "irrelevant" to "might apply" — which is the
right shape for a feed whose top three slots are the only ones read carefully.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "Judgement",
    "TieBreak",
    "average_precision",
    "bootstrap_ci",
    "brier_score",
    "dcg_at_k",
    "expected_calibration_error",
    "f1",
    "false_negative_rate",
    "false_positive_rate",
    "hit_rate_at_k",
    "kendall_tau",
    "mean_average_precision",
    "mean_reciprocal_rank",
    "ndcg_at_k",
    "pr_auc",
    "precision_at_k",
    "recall_at_k",
    "reciprocal_rank",
    "roc_auc",
    "spearman",
]


class TieBreak(StrEnum):
    """How to order items a scorer could not separate.

    See the module docstring: `PESSIMISTIC` is the default because it is the
    only one a scorer cannot game by returning a constant.
    """

    PESSIMISTIC = "pessimistic"
    OPTIMISTIC = "optimistic"


@dataclass(frozen=True)
class Judgement:
    """One scored item and the relevance a human assigned it.

    `key` is carried so error analysis can name what ranked badly. A metric
    that says 0.61 and cannot say *which* posting cost you the other 0.39 is
    the same dead end as the score it was built to explain.
    """

    score: float
    relevance: int
    key: str = ""

    def __post_init__(self) -> None:
        if self.relevance < 0:
            raise ValueError(f"relevance must be >= 0, got {self.relevance}")


def _ordered(
    judgements: Sequence[Judgement], tie_break: TieBreak = TieBreak.PESSIMISTIC
) -> list[Judgement]:
    """Rank by score descending, resolving ties per `tie_break`."""
    if tie_break is TieBreak.PESSIMISTIC:
        return sorted(judgements, key=lambda j: (-j.score, j.relevance))
    return sorted(judgements, key=lambda j: (-j.score, -j.relevance))


def _relevant_count(judgements: Sequence[Judgement]) -> int:
    return sum(1 for j in judgements if j.relevance > 0)


# ---------------------------------------------------------------------------
# Ranking — "did the good ones come first"
# ---------------------------------------------------------------------------


def precision_at_k(
    judgements: Sequence[Judgement], k: int, *, tie_break: TieBreak = TieBreak.PESSIMISTIC
) -> float:
    """Of the top k, what fraction is relevant.

    The denominator is `k` even when the feed is shorter, so a scorer with
    three items cannot claim P@10 = 1.0 for having no room to be wrong.
    """
    if k <= 0:
        raise ValueError("k must be positive")
    top = _ordered(judgements, tie_break)[:k]
    return sum(1 for j in top if j.relevance > 0) / k


def recall_at_k(
    judgements: Sequence[Judgement], k: int, *, tie_break: TieBreak = TieBreak.PESSIMISTIC
) -> float:
    """Of everything relevant, what fraction reached the top k."""
    if k <= 0:
        raise ValueError("k must be positive")
    total = _relevant_count(judgements)
    if total == 0:
        return 0.0
    top = _ordered(judgements, tie_break)[:k]
    return sum(1 for j in top if j.relevance > 0) / total


def hit_rate_at_k(
    judgements: Sequence[Judgement], k: int, *, tie_break: TieBreak = TieBreak.PESSIMISTIC
) -> float:
    """1.0 if anything relevant reached the top k, else 0.0.

    Coarse on purpose. For a feed the owner scans once a day, "was there
    anything worth reading" is a different question from "how much of it".
    """
    if k <= 0:
        raise ValueError("k must be positive")
    top = _ordered(judgements, tie_break)[:k]
    return 1.0 if any(j.relevance > 0 for j in top) else 0.0


def average_precision(
    judgements: Sequence[Judgement], *, tie_break: TieBreak = TieBreak.PESSIMISTIC
) -> float:
    """Mean of P@i taken at every rank i holding a relevant item.

    Divided by the number of relevant items in the whole judged set, not by
    the number retrieved — a scorer that surfaces one of ten relevant postings
    perfectly has not solved the problem.
    """
    total = _relevant_count(judgements)
    if total == 0:
        return 0.0
    hits = 0
    running = 0.0
    for i, j in enumerate(_ordered(judgements, tie_break), start=1):
        if j.relevance > 0:
            hits += 1
            running += hits / i
    return running / total


def mean_average_precision(
    queries: Sequence[Sequence[Judgement]], *, tie_break: TieBreak = TieBreak.PESSIMISTIC
) -> float:
    """MAP over several rankings. One profile is one query."""
    if not queries:
        return 0.0
    return sum(average_precision(q, tie_break=tie_break) for q in queries) / len(queries)


def reciprocal_rank(
    judgements: Sequence[Judgement], *, tie_break: TieBreak = TieBreak.PESSIMISTIC
) -> float:
    """1 / the rank of the first relevant item; 0.0 if there is none."""
    for i, j in enumerate(_ordered(judgements, tie_break), start=1):
        if j.relevance > 0:
            return 1.0 / i
    return 0.0


def mean_reciprocal_rank(
    queries: Sequence[Sequence[Judgement]], *, tie_break: TieBreak = TieBreak.PESSIMISTIC
) -> float:
    if not queries:
        return 0.0
    return sum(reciprocal_rank(q, tie_break=tie_break) for q in queries) / len(queries)


def dcg_at_k(
    judgements: Sequence[Judgement], k: int, *, tie_break: TieBreak = TieBreak.PESSIMISTIC
) -> float:
    """Discounted cumulative gain: `(2**rel - 1) / log2(rank + 1)`, summed."""
    if k <= 0:
        raise ValueError("k must be positive")
    return sum(
        (2.0**j.relevance - 1.0) / math.log2(i + 1)
        for i, j in enumerate(_ordered(judgements, tie_break)[:k], start=1)
    )


def ndcg_at_k(
    judgements: Sequence[Judgement], k: int, *, tie_break: TieBreak = TieBreak.PESSIMISTIC
) -> float:
    """DCG@k over the DCG@k of the best ordering that existed.

    0.0 when nothing in the set is relevant — not 1.0. There is a defensible
    reading where a scorer cannot be blamed for a query with no right answer,
    but averaged across a set it would let a feed full of hopeless queries
    report a strong number, so the blameless case scores nothing.
    """
    if k <= 0:
        raise ValueError("k must be positive")
    ideal = sorted(judgements, key=lambda j: -j.relevance)[:k]
    best = sum((2.0**j.relevance - 1.0) / math.log2(i + 1) for i, j in enumerate(ideal, start=1))
    if best == 0:
        return 0.0
    return dcg_at_k(judgements, k, tie_break=tie_break) / best


# ---------------------------------------------------------------------------
# Rank correlation — "did the whole ordering agree", not just the head
# ---------------------------------------------------------------------------


def _ranks(values: Sequence[float]) -> list[float]:
    """Ascending ranks, ties sharing their average. 1-based."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2 + 1
        for pos in order[i : j + 1]:
            ranks[pos] = shared
        i = j + 1
    return ranks


def spearman(a: Sequence[float], b: Sequence[float]) -> float:
    """Rank correlation. Pearson over the ranks, so ties are handled."""
    if len(a) != len(b):
        raise ValueError("sequences must be the same length")
    if len(a) < 2:
        return 0.0
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    da = math.sqrt(sum((x - ma) ** 2 for x in ra))
    db = math.sqrt(sum((y - mb) ** 2 for y in rb))
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


def kendall_tau(a: Sequence[float], b: Sequence[float]) -> float:
    """Tau-b: concordant minus discordant pairs, corrected for ties."""
    if len(a) != len(b):
        raise ValueError("sequences must be the same length")
    n = len(a)
    if n < 2:
        return 0.0
    concordant = discordant = ties_a = ties_b = 0
    for i in range(n):
        for j in range(i + 1, n):
            da = a[i] - a[j]
            db = b[i] - b[j]
            product = da * db
            if product > 0:
                concordant += 1
            elif product < 0:
                discordant += 1
            else:
                if da == 0:
                    ties_a += 1
                if db == 0:
                    ties_b += 1
    pairs = n * (n - 1) / 2
    denominator = math.sqrt((pairs - ties_a) * (pairs - ties_b))
    if denominator == 0:
        return 0.0
    return (concordant - discordant) / denominator


# ---------------------------------------------------------------------------
# Classification — "was the decision right", for anything that emits a label
# ---------------------------------------------------------------------------


def _confusion(
    scores: Sequence[float], labels: Sequence[int], threshold: float
) -> tuple[int, int, int, int]:
    """(tp, fp, tn, fn) at a threshold. `>=` counts as positive."""
    if len(scores) != len(labels):
        raise ValueError("scores and labels must be the same length")
    tp = fp = tn = fn = 0
    for s, y in zip(scores, labels, strict=True):
        predicted = s >= threshold
        if predicted and y > 0:
            tp += 1
        elif predicted:
            fp += 1
        elif y > 0:
            fn += 1
        else:
            tn += 1
    return tp, fp, tn, fn


def precision(scores: Sequence[float], labels: Sequence[int], threshold: float) -> float:
    tp, fp, _, _ = _confusion(scores, labels, threshold)
    return tp / (tp + fp) if tp + fp else 0.0


def recall(scores: Sequence[float], labels: Sequence[int], threshold: float) -> float:
    tp, _, _, fn = _confusion(scores, labels, threshold)
    return tp / (tp + fn) if tp + fn else 0.0


def f1(scores: Sequence[float], labels: Sequence[int], threshold: float) -> float:
    p = precision(scores, labels, threshold)
    r = recall(scores, labels, threshold)
    return 2 * p * r / (p + r) if p + r else 0.0


def accuracy(scores: Sequence[float], labels: Sequence[int], threshold: float) -> float:
    tp, fp, tn, fn = _confusion(scores, labels, threshold)
    total = tp + fp + tn + fn
    return (tp + tn) / total if total else 0.0


def false_positive_rate(scores: Sequence[float], labels: Sequence[int], threshold: float) -> float:
    _, fp, tn, _ = _confusion(scores, labels, threshold)
    return fp / (fp + tn) if fp + tn else 0.0


def false_negative_rate(scores: Sequence[float], labels: Sequence[int], threshold: float) -> float:
    tp, _, _, fn = _confusion(scores, labels, threshold)
    return fn / (fn + tp) if fn + tp else 0.0


def roc_auc(scores: Sequence[float], labels: Sequence[int]) -> float:
    """Area under the ROC curve, by the Mann–Whitney U identity.

    Computed from average ranks rather than by walking a curve, so tied scores
    contribute 0.5 apiece exactly — the case that matters here, because the
    lexical embedder returns 0.0 for a posting sharing no token with the
    profile and there are usually several of them.
    """
    if len(scores) != len(labels):
        raise ValueError("scores and labels must be the same length")
    positives = sum(1 for y in labels if y > 0)
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        return 0.0
    ranks = _ranks(scores)
    rank_sum = sum(r for r, y in zip(ranks, labels, strict=True) if y > 0)
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def pr_auc(scores: Sequence[float], labels: Sequence[int]) -> float:
    """Average precision — the step-wise area under the precision/recall curve.

    Preferred over ROC-AUC when positives are rare, which is the job feed's
    normal condition: a crawl returns hundreds of postings and the owner wants
    five of them.
    """
    if len(scores) != len(labels):
        raise ValueError("scores and labels must be the same length")
    positives = sum(1 for y in labels if y > 0)
    if positives == 0:
        return 0.0
    pairs = sorted(zip(scores, labels, strict=True), key=lambda p: -p[0])
    area = 0.0
    tp = fp = 0
    previous_recall = 0.0
    i = 0
    while i < len(pairs):
        j = i
        # Everything on one score is one threshold; splitting a tie group
        # would invent an ordering the scorer never expressed.
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        for _, y in pairs[i : j + 1]:
            if y > 0:
                tp += 1
            else:
                fp += 1
        current_recall = tp / positives
        current_precision = tp / (tp + fp)
        area += (current_recall - previous_recall) * current_precision
        previous_recall = current_recall
        i = j + 1
    return area


def expected_calibration_error(
    probabilities: Sequence[float], labels: Sequence[int], *, bins: int = 10
) -> float:
    """Mean gap between claimed confidence and observed frequency.

    A score is calibrated when the postings it calls 0.8 are wanted about 80%
    of the time. Ranking metrics are blind to this — an ordering is unchanged
    by any monotone squashing of the scores — and `Profile.min_match_score`
    compares a raw number against a threshold, so the units have to mean
    something for that comparison to.
    """
    if len(probabilities) != len(labels):
        raise ValueError("probabilities and labels must be the same length")
    if bins <= 0:
        raise ValueError("bins must be positive")
    if not probabilities:
        return 0.0
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for p, y in zip(probabilities, labels, strict=True):
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"probability out of range: {p}")
        index = min(int(p * bins), bins - 1)
        buckets[index].append((p, y))
    total = len(probabilities)
    error = 0.0
    for bucket in buckets:
        if not bucket:
            continue
        confidence = sum(p for p, _ in bucket) / len(bucket)
        observed = sum(1 for _, y in bucket if y > 0) / len(bucket)
        error += len(bucket) / total * abs(confidence - observed)
    return error


def brier_score(probabilities: Sequence[float], labels: Sequence[int]) -> float:
    """Mean squared error of the probabilities. Lower is better."""
    if len(probabilities) != len(labels):
        raise ValueError("probabilities and labels must be the same length")
    if not probabilities:
        return 0.0
    return sum(
        (p - (1.0 if y > 0 else 0.0)) ** 2 for p, y in zip(probabilities, labels, strict=True)
    ) / len(probabilities)


# ---------------------------------------------------------------------------
# Is the difference real
# ---------------------------------------------------------------------------


def bootstrap_ci(
    items: Sequence[object],
    metric: Callable[[Sequence[object]], float],
    *,
    samples: int = 1000,
    confidence: float = 0.95,
    seed: int = 0,
) -> tuple[float, float]:
    """Percentile bootstrap interval for a metric over `items`.

    The reason this exists rather than a bare comparison: on a twenty-posting
    set the interval around NDCG@10 is wide enough to swallow most differences
    two scorers will show. Reporting 0.83 against 0.81 as an improvement is the
    inflated metric CLAUDE.md §2 and the master spec both forbid, and the only
    way to know it is not one is to compute how much the number moves when the
    set is resampled.

    Seeded, because a confidence interval that changes between runs is one
    more thing to argue about.
    """
    if not items:
        return (0.0, 0.0)
    if samples <= 0:
        raise ValueError("samples must be positive")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between 0 and 1")
    rng = random.Random(seed)
    n = len(items)
    draws = sorted(metric([items[rng.randrange(n)] for _ in range(n)]) for _ in range(samples))
    tail = (1.0 - confidence) / 2
    low = draws[min(int(tail * samples), samples - 1)]
    high = draws[min(int((1.0 - tail) * samples), samples - 1)]
    return (low, high)
