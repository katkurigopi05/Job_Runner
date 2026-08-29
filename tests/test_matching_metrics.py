"""Metrics, checked against values computed by hand.

Every expected number in this file was worked out on paper from the
definition, not read off the implementation. A metric test that asserts the
code agrees with itself is the same dead end as an LLM grading its own
rewrite, and `packages/matching/metrics.py` exists precisely to stop that
pattern elsewhere.

Where a value is not obvious the arithmetic is in the docstring, so a reader
who disagrees can check the claim rather than the code.
"""

from __future__ import annotations

import pytest

from packages.matching.benchmark import (
    default_variants,
    run_variant,
    summarize,
)
from packages.matching.labels import (
    LabeledPosting,
    LabeledSet,
    Provenance,
    load_labeled_set,
)
from packages.matching.metrics import (
    Judgement,
    TieBreak,
    average_precision,
    bootstrap_ci,
    brier_score,
    dcg_at_k,
    expected_calibration_error,
    f1,
    false_negative_rate,
    false_positive_rate,
    hit_rate_at_k,
    kendall_tau,
    mean_average_precision,
    mean_reciprocal_rank,
    ndcg_at_k,
    pr_auc,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    roc_auc,
    spearman,
)

LABELED_SET = "seeds/labeled_matches.yaml"


def js(*pairs: tuple[float, int]) -> list[Judgement]:
    return [Judgement(score=s, relevance=r, key=f"k{i}") for i, (s, r) in enumerate(pairs)]


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def test_precision_at_k_divides_by_k_not_by_the_feed_length() -> None:
    """Three items, two relevant, P@10 is 0.2 — not 0.667.

    A short feed must not score well for having had no room to be wrong.
    """
    assert precision_at_k(js((0.9, 1), (0.8, 1), (0.7, 0)), 10) == pytest.approx(0.2)
    assert precision_at_k(js((0.9, 1), (0.8, 1), (0.7, 0)), 2) == pytest.approx(1.0)


def test_recall_at_k_divides_by_everything_relevant() -> None:
    items = js((0.9, 1), (0.8, 0), (0.7, 1), (0.6, 1))
    assert recall_at_k(items, 1) == pytest.approx(1 / 3)
    assert recall_at_k(items, 4) == pytest.approx(1.0)


def test_recall_is_zero_when_nothing_is_relevant() -> None:
    assert recall_at_k(js((0.9, 0), (0.1, 0)), 2) == 0.0


def test_hit_rate_is_a_bit_not_a_fraction() -> None:
    assert hit_rate_at_k(js((0.9, 0), (0.8, 1)), 2) == 1.0
    assert hit_rate_at_k(js((0.9, 0), (0.8, 1)), 1) == 0.0


def test_average_precision_worked_example() -> None:
    """Relevant at ranks 1 and 3 of 3: (1/1 + 2/3) / 2 = 0.8333."""
    assert average_precision(js((0.9, 1), (0.8, 0), (0.7, 1))) == pytest.approx(5 / 6)


def test_average_precision_penalises_burying_a_relevant_item() -> None:
    early = average_precision(js((0.9, 1), (0.5, 0), (0.4, 0)))
    late = average_precision(js((0.9, 0), (0.5, 0), (0.4, 1)))
    assert early == pytest.approx(1.0)
    assert late == pytest.approx(1 / 3)


def test_reciprocal_rank_and_mrr() -> None:
    assert reciprocal_rank(js((0.9, 0), (0.8, 1))) == pytest.approx(0.5)
    assert reciprocal_rank(js((0.9, 0), (0.8, 0))) == 0.0
    queries = [js((0.9, 1)), js((0.9, 0), (0.8, 0), (0.7, 1))]
    assert mean_reciprocal_rank(queries) == pytest.approx((1.0 + 1 / 3) / 2)


def test_map_averages_over_queries() -> None:
    assert mean_average_precision([js((0.9, 1)), js((0.9, 0), (0.8, 1))]) == pytest.approx(0.75)


def test_dcg_worked_example() -> None:
    """rel 1 at rank 1 and rel 1 at rank 3: 1/log2(2) + 1/log2(4) = 1.5."""
    assert dcg_at_k(js((0.9, 1), (0.8, 0), (0.7, 1)), 3) == pytest.approx(1.5)


def test_dcg_gain_is_exponential_in_the_grade() -> None:
    """A grade-3 item at rank 1 is worth 2**3 - 1 = 7, not 3."""
    assert dcg_at_k(
        js(
            (0.9, 3),
        ),
        1,
    ) == pytest.approx(7.0)
    assert dcg_at_k(
        js(
            (0.9, 1),
        ),
        1,
    ) == pytest.approx(1.0)


def test_ndcg_worked_example() -> None:
    """DCG 1.5 over an ideal of 1 + 1/log2(3) = 1.63093 → 0.91972."""
    assert ndcg_at_k(js((0.9, 1), (0.8, 0), (0.7, 1)), 3) == pytest.approx(0.919721, abs=1e-6)


def test_ndcg_is_one_for_a_perfect_ordering() -> None:
    assert ndcg_at_k(js((0.9, 3), (0.8, 2), (0.7, 0)), 3) == pytest.approx(1.0)


def test_ndcg_is_zero_when_nothing_is_relevant() -> None:
    """Not 1.0. See the docstring: a blameless query must not flatter an average."""
    assert ndcg_at_k(js((0.9, 0), (0.8, 0)), 2) == 0.0


def test_a_constant_scorer_cannot_earn_credit_from_input_order() -> None:
    """The tie-break rule that stops a null model from looking perfect.

    Every score identical and the relevant item listed first. Under a stable
    sort this is a flawless ranking; under `PESSIMISTIC` the relevant item
    goes last, giving DCG = 1/log2(3) = 0.63093 over an ideal of 1.0.
    """
    tied = js((0.5, 1), (0.5, 0))
    assert ndcg_at_k(tied, 2) == pytest.approx(0.630930, abs=1e-6)
    assert ndcg_at_k(tied, 2, tie_break=TieBreak.OPTIMISTIC) == pytest.approx(1.0)
    assert precision_at_k(tied, 1) == 0.0


def test_the_two_tie_policies_agree_when_there_are_no_ties() -> None:
    distinct = js((0.9, 1), (0.5, 0), (0.1, 1))
    assert ndcg_at_k(distinct, 3) == ndcg_at_k(distinct, 3, tie_break=TieBreak.OPTIMISTIC)


def test_k_must_be_positive() -> None:
    for metric in (precision_at_k, recall_at_k, hit_rate_at_k, dcg_at_k, ndcg_at_k):
        with pytest.raises(ValueError):
            metric(js((0.9, 1)), 0)


def test_relevance_cannot_be_negative() -> None:
    with pytest.raises(ValueError):
        Judgement(score=0.5, relevance=-1)


# ---------------------------------------------------------------------------
# Rank correlation
# ---------------------------------------------------------------------------


def test_spearman_is_one_for_a_monotone_relationship() -> None:
    """Rank correlation, so a non-linear but order-preserving map is still 1."""
    assert spearman([1, 2, 3, 4], [1, 4, 9, 16]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)


def test_spearman_handles_ties_through_average_ranks() -> None:
    assert spearman([1, 1, 2], [5, 5, 9]) == pytest.approx(1.0)


def test_spearman_of_a_constant_is_zero_not_undefined() -> None:
    assert spearman([1, 1, 1], [1, 2, 3]) == 0.0


def test_kendall_tau_worked_example() -> None:
    """One discordant pair of three: (2 - 1) / 3 = 0.3333."""
    assert kendall_tau([1, 2, 3], [1, 3, 2]) == pytest.approx(1 / 3)
    assert kendall_tau([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)


def test_correlations_reject_mismatched_lengths() -> None:
    for metric in (spearman, kendall_tau):
        with pytest.raises(ValueError):
            metric([1, 2], [1])


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_confusion_derived_metrics_worked_example() -> None:
    """At threshold 0.5: tp=2, fp=1, tn=1, fn=1."""
    scores = [0.9, 0.7, 0.6, 0.4, 0.1]
    labels = [1, 1, 0, 1, 0]
    # precision = 2/3, recall = 2/3, so F1 = 2/3.
    assert f1(scores, labels, 0.5) == pytest.approx(2 / 3)
    assert false_positive_rate(scores, labels, 0.5) == pytest.approx(0.5)
    assert false_negative_rate(scores, labels, 0.5) == pytest.approx(1 / 3)


def test_roc_auc_worked_example() -> None:
    """Positives at ranks 4 and 3 of 4 ascending → (7 - 3) / 4 = 1.0 … then a miss."""
    assert roc_auc([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0]) == pytest.approx(1.0)
    assert roc_auc([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1]) == pytest.approx(0.0)
    assert roc_auc([0.9, 0.2, 0.8, 0.1], [1, 1, 0, 0]) == pytest.approx(0.75)


def test_roc_auc_scores_all_ties_at_one_half() -> None:
    """The lexical embedder returns 0.0 for several postings at once."""
    assert roc_auc([0.5, 0.5, 0.5, 0.5], [1, 1, 0, 0]) == pytest.approx(0.5)


def test_roc_auc_needs_both_classes() -> None:
    assert roc_auc([0.9, 0.8], [1, 1]) == 0.0


def test_pr_auc_worked_example() -> None:
    """Perfect separation is 1.0; an inverted ranking of two of four is 0.5."""
    assert pr_auc([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0]) == pytest.approx(1.0)
    # ranks: pos, neg, pos, neg → 1*0.5 + 0.5*(2/3) = 0.8333
    assert pr_auc([0.9, 0.8, 0.7, 0.6], [1, 0, 1, 0]) == pytest.approx(1 / 2 + (1 / 2) * (2 / 3))


def test_pr_auc_treats_a_tie_group_as_one_threshold() -> None:
    """Splitting a tie would invent an ordering the scorer never expressed."""
    assert pr_auc([0.5, 0.5], [1, 0]) == pytest.approx(0.5)


def test_expected_calibration_error_is_zero_when_confidence_matches_reality() -> None:
    """Ten items at p=0.9, nine of them positive. Perfectly calibrated."""
    probabilities = [0.9] * 10
    labels = [1] * 9 + [0]
    assert expected_calibration_error(probabilities, labels, bins=10) == pytest.approx(0.0)


def test_expected_calibration_error_catches_overconfidence() -> None:
    """Claims 1.0, is right half the time. ECE = 0.5."""
    assert expected_calibration_error([1.0] * 4, [1, 1, 0, 0]) == pytest.approx(0.5)


def test_calibration_rejects_a_score_that_is_not_a_probability() -> None:
    with pytest.raises(ValueError):
        expected_calibration_error([1.4], [1])


def test_brier_score() -> None:
    assert brier_score([1.0, 0.0], [1, 0]) == pytest.approx(0.0)
    assert brier_score([0.0, 1.0], [1, 0]) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_interval_brackets_the_point_estimate() -> None:
    items = js(*[(0.9 - i * 0.05, 1 if i < 5 else 0) for i in range(20)])

    def metric(sample) -> float:
        return ndcg_at_k([j for j in sample if isinstance(j, Judgement)], 10)

    low, high = bootstrap_ci(items, metric, samples=300, seed=3)
    assert low <= ndcg_at_k(items, 10) <= high


def test_bootstrap_is_seeded() -> None:
    items = js((0.9, 1), (0.5, 0), (0.2, 1))

    def metric(sample) -> float:
        return ndcg_at_k([j for j in sample if isinstance(j, Judgement)], 3)

    assert bootstrap_ci(items, metric, samples=100, seed=7) == bootstrap_ci(
        items, metric, samples=100, seed=7
    )


def test_bootstrap_on_an_empty_set_is_zero_not_a_crash() -> None:
    assert bootstrap_ci([], lambda _: 1.0) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# Labeled sets
# ---------------------------------------------------------------------------


def test_the_shipped_labeled_set_loads() -> None:
    dataset = load_labeled_set(LABELED_SET)
    assert len(dataset.items) >= 32
    assert len(dataset.tagged("gate5")) == 20, (
        "Gate 5 asserts '10 of 20'. Adding to that tag changes what the gate proves."
    )
    assert dataset.tagged("adjacent")


def test_the_shipped_set_declares_itself_fixture_only() -> None:
    """The honest state of the world today, asserted so a change is deliberate.

    When the owner labels real postings this test fails, and the fix is to
    update it — not to quietly keep claiming the numbers are synthetic.
    """
    dataset = load_labeled_set(LABELED_SET)
    assert dataset.is_fixture_only
    assert dataset.provenance_mix == {"fixture": len(dataset.items)}


def test_the_digest_tracks_the_labels() -> None:
    dataset = load_labeled_set(LABELED_SET)
    first = dataset.items[0]
    moved = LabeledSet(
        name=dataset.name,
        version=dataset.version,
        profile_text=dataset.profile_text,
        items=(
            LabeledPosting(
                key=first.key,
                title=first.title,
                description=first.description,
                relevance=0 if first.relevance else 3,
                provenance=first.provenance,
            ),
            *dataset.items[1:],
        ),
    )
    assert moved.digest != dataset.digest


def test_the_digest_ignores_ordering() -> None:
    dataset = load_labeled_set(LABELED_SET)
    reversed_set = LabeledSet(
        name=dataset.name,
        version=dataset.version,
        profile_text=dataset.profile_text,
        items=tuple(reversed(dataset.items)),
    )
    assert reversed_set.digest == dataset.digest


def test_a_missing_provenance_is_refused() -> None:
    """A default would be `fixture`, which understates a real label set."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bad.yaml"
        path.write_text(
            "name: x\nversion: '1'\nprofile_text: hi\n"
            "postings:\n  - key: a\n    title: t\n    description: d\n    relevance: 1\n"
        )
        with pytest.raises(ValueError, match="provenance"):
            load_labeled_set(path)


def test_an_unknown_provenance_is_refused() -> None:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bad.yaml"
        path.write_text(
            "name: x\nversion: '1'\nprofile_text: hi\npostings:\n"
            "  - key: a\n    title: t\n    description: d\n    relevance: 1\n"
            "    provenance: vibes\n"
        )
        with pytest.raises(ValueError, match="unknown provenance"):
            load_labeled_set(path)


def test_duplicate_keys_are_refused() -> None:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "dupe.yaml"
        path.write_text(
            "name: x\nversion: '1'\nprofile_text: hi\npostings:\n"
            "  - {key: a, title: t, description: d, relevance: 1, provenance: fixture}\n"
            "  - {key: a, title: u, description: e, relevance: 0, provenance: fixture}\n"
        )
        with pytest.raises(ValueError, match="duplicate key"):
            load_labeled_set(path)


def test_relevance_outside_the_scale_is_refused() -> None:
    with pytest.raises(ValueError, match="outside the scale"):
        LabeledPosting(
            key="a",
            title="t",
            description="d",
            relevance=9,
            provenance=Provenance.FIXTURE,
        )


def test_split_never_puts_a_company_on_both_sides() -> None:
    """Company leakage is the easy one to introduce and the hard one to see."""
    items = tuple(
        LabeledPosting(
            key=f"p{i}",
            title=f"Engineer {i}",
            description="python postgresql",
            relevance=i % 2,
            provenance=Provenance.FIXTURE,
            company=f"company-{i % 4}",
        )
        for i in range(20)
    )
    dataset = LabeledSet(name="x", version="1", profile_text="python", items=items)
    train, holdout = dataset.split(holdout=0.3, seed=1)

    assert train and holdout
    assert len(train) + len(holdout) == len(items)
    left = {i.company for i in train}
    right = {i.company for i in holdout}
    assert not (left & right), f"company on both sides: {left & right}"


def test_split_is_deterministic() -> None:
    dataset = load_labeled_set(LABELED_SET)
    assert dataset.split(seed=4) == dataset.split(seed=4)


def test_ungrouped_postings_do_not_collapse_into_one_group() -> None:
    """The shipped set has no company field; it must still be splittable."""
    dataset = load_labeled_set(LABELED_SET)
    train, holdout = dataset.split(holdout=0.5, seed=0)
    assert train and holdout


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------


def test_every_variant_runs_and_records_its_dataset() -> None:
    dataset = load_labeled_set(LABELED_SET)
    for variant in default_variants():
        record = run_variant(variant, dataset)
        assert record.dataset_digest == dataset.digest
        assert record.dataset_version == dataset.version
        assert record.fixture_only is True
        assert 0.0 <= record.metrics["ndcg@10"] <= 1.0
        assert record.latency_ms_per_item >= 0.0


def test_the_control_ranks_at_zero() -> None:
    """If this ever passes with a real number, the tie-break has been undone."""
    dataset = load_labeled_set(LABELED_SET)
    control = next(v for v in default_variants() if v.name == "constant")
    record = run_variant(control, dataset)
    assert record.metrics["ndcg@10"] == 0.0
    assert record.metrics["roc_auc"] == pytest.approx(0.5)


def test_the_shipped_scorer_beats_the_control() -> None:
    """The weakest claim worth making, and the one that must never regress."""
    dataset = load_labeled_set(LABELED_SET)
    variants = {v.name: v for v in default_variants()}
    production = run_variant(variants["production"], dataset)
    control = run_variant(variants["constant"], dataset)
    assert production.metrics["ndcg@10"] > control.metrics["ndcg@10"] + 0.5


def test_fixture_only_labels_block_a_production_claim() -> None:
    """§45 and CLAUDE.md §15, enforced rather than documented.

    No benchmark run over synthetic labels may report a production candidate,
    however good the numbers look.
    """
    dataset = load_labeled_set(LABELED_SET)
    verdict = summarize([run_variant(v, dataset) for v in default_variants()])
    assert verdict.production_candidate is False
    assert any("fixture-only" in b for b in verdict.blockers)


def test_a_leading_control_is_called_out() -> None:
    """If the null model wins, the summary must say so in as many words."""
    dataset = load_labeled_set(LABELED_SET)
    control = next(v for v in default_variants() if v.name == "constant")
    verdict = summarize([run_variant(control, dataset)])
    assert verdict.best == "constant"
    assert any("constant control leads" in b for b in verdict.blockers)


def test_variants_that_cannot_be_separated_are_reported_as_tied() -> None:
    """32 labels cannot separate the shipped scorer from token overlap.

    This is the finding, asserted so that a future change which *does*
    separate them is visible as a change rather than as a passing test.
    """
    dataset = load_labeled_set(LABELED_SET)
    verdict = summarize([run_variant(v, dataset) for v in default_variants()])
    assert "jaccard" in verdict.tied_with_best


def test_the_original_gate5_labels_cannot_tell_the_variants_apart() -> None:
    """Why the adjacent roles were added.

    On the twenty Gate 5 postings the shipped scorer and a five-line token
    overlap both score a perfect NDCG@10. A set whose negatives are pastry
    chefs and truck drivers measures nothing about ranking quality.
    """
    dataset = load_labeled_set(LABELED_SET)
    gate5 = dataset.tagged("gate5")
    variants = {v.name: v for v in default_variants()}
    production = run_variant(variants["production"], dataset, gate5)
    jaccard = run_variant(variants["jaccard"], dataset, gate5)
    assert production.metrics["ndcg@10"] == pytest.approx(1.0)
    assert jaccard.metrics["ndcg@10"] == pytest.approx(1.0)


def test_the_adjacent_roles_are_where_the_scorer_struggles() -> None:
    """And the honest counterpart: on hard cases nothing here is convincing."""
    dataset = load_labeled_set(LABELED_SET)
    adjacent = dataset.tagged("adjacent")
    variants = {v.name: v for v in default_variants()}
    production = run_variant(variants["production"], dataset, adjacent, k=5)
    assert production.metrics["ndcg@5"] < 0.8


def test_arming_the_seniority_filter_removes_the_junior_posting() -> None:
    """`filters.seniority_ok` passes everything when no target is given.

    The shipped feed does not set one, so a Junior Backend Engineer with a
    perfect technology match ranks in the top ten. Measured here so the cost
    of that default is a number rather than an opinion.
    """
    dataset = load_labeled_set(LABELED_SET)
    variants = {v.name: v for v in default_variants()}
    off = run_variant(variants["production"], dataset)
    on = run_variant(variants["production+seniority"], dataset)
    assert "adj-junior-backend" in off.false_positives
    assert "adj-junior-backend" not in on.false_positives
    assert on.metrics["precision@10"] > off.metrics["precision@10"]


def test_a_variant_never_sees_the_relevance_label() -> None:
    """§45: an evaluator that can read the grade is grading itself.

    Checked structurally — the callable takes the profile text and a posting,
    and a `LabeledPosting` with its grade flipped must score identically.
    """
    dataset = load_labeled_set(LABELED_SET)
    item = dataset.items[0]
    flipped = LabeledPosting(
        key=item.key,
        title=item.title,
        description=item.description,
        relevance=0 if item.relevance else 3,
        provenance=item.provenance,
        company=item.company,
        location=item.location,
    )
    for variant in default_variants():
        assert variant.score(dataset.profile_text, item) == variant.score(
            dataset.profile_text, flipped
        ), f"{variant.name} changed its score when only the label changed"


def test_summarize_needs_something_to_summarize() -> None:
    with pytest.raises(ValueError):
        summarize([])


def test_records_serialize() -> None:
    import json

    dataset = load_labeled_set(LABELED_SET)
    record = run_variant(default_variants()[0], dataset)
    assert json.loads(json.dumps(record.as_dict()))["dataset"]["digest"] == dataset.digest
