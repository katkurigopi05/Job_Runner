"""RRF fusion, and the guard that stops its result being misread.

The arithmetic here is short enough that the interesting tests are not about
BM25 being BM25. They are about the two ways a fusion experiment lies:

- **Fusing a signal with itself.** RRF combines rankings that fail
  differently. Handed two views of the same information it cannot help, and
  the number it produces still looks like a measurement. That is not a
  hypothetical — it is what the first real run of this module did, because
  `sentence-transformers` is absent and `embed.get_embedder` falls back to
  `LexicalEmbedder`, a hashed bag of words. Spearman +0.916 against BM25.
- **A missing document treated as a last-place document.** One signal's
  cutoff then vetoes the other signal's first pick.
"""

from __future__ import annotations

from packages.matching.hybrid import (
    BM25,
    DEGENERATE_AGREEMENT,
    RRF_K,
    Ranking,
    agreement,
    agreement_note,
    fuse,
)

DOCS = [
    ("python", "Senior Backend Engineer. Python, FastAPI, Postgres, Kubernetes."),
    ("java", "Senior Backend Engineer. Java, Spring Boot, Postgres, Kubernetes."),
    ("chef", "Pastry Chef. Laminated doughs, viennoiserie, early mornings."),
]


# --- BM25 -------------------------------------------------------------------


def test_the_term_that_differs_is_the_term_that_decides() -> None:
    """The whole reason to add a lexical signal to a dense one.

    These two postings differ in a handful of tokens out of a few dozen and
    are otherwise the same sentence. An embedding collapses that difference
    on purpose; exact term identity is what separates them.
    """
    scores = dict(BM25(DOCS).scores("python fastapi"))
    assert scores["python"] > scores["java"] > 0.0 or scores["java"] == 0.0
    assert scores["python"] > scores["chef"]


def test_no_term_can_score_a_document_down() -> None:
    """IDF is never negative, so a common term is worth little, never less.

    Were it negative, a stopword the tokenizer missed would *subtract* from
    every document containing it, and the ranking would invert on the
    corpus's own vocabulary. Lucene's `+ 1.0` inside the log is what
    guarantees it — checked here across every document-frequency a corpus of
    this size can produce, rather than on the one corpus above.
    """
    bm25 = BM25(DOCS)
    assert all(value >= 0.0 for value in bm25.idf.values())
    # A term in every document is the worst case and is still non-negative,
    # which is why `hybrid.py` carries no `max(0.0, ...)` clamp: it could
    # never fire.
    everywhere = BM25([("a", "python alpha"), ("b", "python beta"), ("c", "python gamma")])
    assert everywhere.idf["python"] > 0.0
    assert everywhere.idf["python"] < everywhere.idf["alpha"]


def test_repeating_a_term_saturates() -> None:
    """The property that makes BM25 better than raw term frequency here.

    §15 records the ATS scorer reading a posting's sales pitch because it
    ranked by frequency: a company name recurs throughout and a technology is
    named once. Saturation is what stops the recurring word from winning.
    """
    once = BM25([("a", "python engineer"), ("b", "chef")])
    many = BM25([("a", "python python python python python engineer"), ("b", "chef")])
    gain = dict(many.scores("python"))["a"] / dict(once.scores("python"))["a"]
    assert 1.0 < gain < 5.0, f"five occurrences multiplied the score by {gain:.2f}"


# --- fusion -----------------------------------------------------------------


def test_fusion_reads_ranks_and_never_scores() -> None:
    """A cosine in [0,1] and an unbounded BM25 have no common scale.

    Both rankings below put `b` first by an enormous score margin on one side
    and a tiny one on the other. RRF sees two first places, so the margins
    cannot matter — which is the property that means no weight had to be
    fitted to the labels this is reported on.
    """
    huge = Ranking.of("huge", [("a", 0.0), ("b", 10_000.0)])
    tiny = Ranking.of("tiny", [("a", 0.001), ("b", 0.002)])
    assert huge.ranks == tiny.ranks
    assert fuse([huge]) == fuse([tiny])


def test_a_document_one_side_never_returned_is_not_ranked_last() -> None:
    """A vector search with a TOP_N returns a cutoff, not a full ordering.

    Scoring the absent document as though it came last would let that cutoff
    veto the other signal's first place. It contributes nothing from the side
    that did not see it, and keeps what the other side gave it.
    """
    dense = Ranking.of("dense", [("kept", 0.9)])  # "dropped" below the cutoff
    lexical = Ranking.of("bm25", [("dropped", 5.0), ("kept", 0.1)])
    fused = fuse([dense, lexical])

    assert fused["dropped"] == 1.0 / (RRF_K + 1)
    assert fused["kept"] == 1.0 / (RRF_K + 1) + 1.0 / (RRF_K + 2)
    assert fused["kept"] > fused["dropped"], "the cutoff vetoed a top lexical hit"


def test_ties_break_deterministically() -> None:
    """BM25 hands out many exact zeros on a narrow query.

    If their order moved between runs so would the fused result, and a
    benchmark whose ordering is not reproducible measures the tie-break.
    """
    pairs = [("c", 0.0), ("a", 0.0), ("b", 0.0)]
    assert Ranking.of("x", pairs).ranks == Ranking.of("x", list(reversed(pairs))).ranks


# --- the guard --------------------------------------------------------------


def test_a_signal_fused_with_itself_is_reported_as_degenerate() -> None:
    """The case that actually occurred, and the reason `agreement` exists.

    A fused metric is uninterpretable without this number. `hybrid_rrf`
    scoring below the dense side alone reads as "hybrid retrieval does not
    help" — a conclusion about retrieval drawn from a run that only ever had
    one signal in it.
    """
    one = Ranking.of("a", [("x", 0.9), ("y", 0.5), ("z", 0.1)])
    assert agreement(one, one) == 1.0
    assert agreement(one, one) >= DEGENERATE_AGREEMENT


def test_signals_that_disagree_are_not_flagged() -> None:
    forward = Ranking.of("a", [("x", 0.9), ("y", 0.5), ("z", 0.1)])
    reverse = Ranking.of("b", [("x", 0.1), ("y", 0.5), ("z", 0.9)])
    assert agreement(forward, reverse) == -1.0
    assert agreement(forward, reverse) < DEGENERATE_AGREEMENT


def test_a_real_embedder_is_not_told_to_install_itself() -> None:
    """High agreement has two causes, and the remedies point opposite ways.

    Measured with bge-small loaded: the Gate 5 slice agrees at +0.875 because
    off-domain negatives rank last under any scorer. The first version of
    this message told that reader to install the `embeddings` extra, which
    was already installed, so they went to fix the wrong thing.
    """
    lexical = agreement_note(0.916, lexical_embedder=True)
    real = agreement_note(0.875, lexical_embedder=False)

    assert lexical is not None and "embeddings" in lexical
    assert real is not None and "embeddings" not in real
    assert "--tag adjacent" in real


def test_a_real_fusion_carries_no_warning() -> None:
    """The adjacent slice with bge-small, +0.573: a measurement, read as one."""
    assert agreement_note(0.573, lexical_embedder=False) is None
    assert agreement_note(DEGENERATE_AGREEMENT - 0.001, lexical_embedder=True) is None


def test_an_undefined_correlation_is_zero_rather_than_an_error() -> None:
    """One key, or one side ranking everything the same. Neither is a crash."""
    assert agreement(Ranking.of("a", [("x", 1.0)]), Ranking.of("b", [("x", 1.0)])) == 0.0
    assert agreement(Ranking.of("a", []), Ranking.of("b", [])) == 0.0


def test_the_benchmark_records_the_agreement_it_measured() -> None:
    """Recorded on the variant, not just computed and printed.

    `--json` writes these records, so the number that makes the row readable
    has to survive into the file alongside it.
    """
    from packages.matching.benchmark import default_variants, run_variant
    from packages.matching.labels import load_labeled_set

    dataset = load_labeled_set("seeds/labeled_matches.yaml")
    items = dataset.tagged("adjacent")
    variants = {v.name: v for v in default_variants(None, items)}
    assert "hybrid_rrf" in variants, "the fusion variant is missing when items are supplied"

    record = run_variant(variants["hybrid_rrf"], dataset, items, k=5)
    measured = record.hyperparameters["rank_agreement"]
    assert measured, "the variant ran without recording what it fused"

    # The environment this runs in has no dense embedder, so this is expected
    # to be the degenerate case. Asserting the value would pin an accident of
    # the fixture; asserting it was *measured* is the property that matters.
    assert all(-1.0 <= value <= 1.0 for value in measured)


def test_the_fusion_variants_are_absent_rather_than_degraded_without_a_corpus() -> None:
    """There is no such thing as the rank of one posting on its own.

    A caller that cannot supply the candidate set gets a shorter table, not a
    fusion variant quietly scoring something else.
    """
    from packages.matching.benchmark import default_variants

    names = {v.name for v in default_variants(None)}
    assert "hybrid_rrf" not in names
    assert "bm25_only" not in names
    assert "production" in names
