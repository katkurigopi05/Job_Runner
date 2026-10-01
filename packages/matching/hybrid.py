"""Reciprocal Rank Fusion over a dense ranking and a lexical one.

`docs/ML_EVALUATION.md` and CLAUDE.md §15 both record where the matcher is
weak, and it is not the obvious place. On the twenty Gate 5 postings the
shipped scorer and a five-line token-overlap baseline both reach a perfect
NDCG@10, because the negatives there are pastry chefs and truck drivers. The
twelve *adjacent* roles are where it falls over: NDCG@5 of 0.577 on the
lexical fallback and 0.405 on bge-small, with the constant control
statistically tied against everything.

A tie with a control that returns 0.5 for every posting means the scorer is
contributing nothing on exactly the comparisons that decide a real feed.

**Why a dense vector is weakest there.** `score.py` is a weighted cosine over
384 dimensions, and embeddings earn their keep by collapsing surface form —
"Member of Technical Staff" landing near "Software Engineer" is the feature
`roles.py` exists to exploit. The same collapse is the problem between a
Python backend role and a Java one: they differ in a handful of tokens out of
several hundred, the sentences around those tokens are near-identical, and the
cosine cannot see the difference. Exact term identity is what separates them,
and that is the one thing a bag of hashed floats has thrown away.

**The repo already holds both signals and uses them as alternatives.**
`embed.get_embedder` selects `SentenceTransformerEmbedder` *or* falls back to
`LexicalEmbedder` inside a try/except — dense or lexical, never both. Fusion
is what turns that either/or into a both.

## Why RRF rather than adding the scores

The two signals are on incomparable scales. A cosine lives in [0, 1] and, on
this corpus, occupies a sliver of it — §15 records a real run over 10,922
postings peaking at 0.271. BM25 is unbounded and depends on corpus statistics.
Adding them with a weight means tuning that weight on the same labels used to
report the result, which is the self-evaluating referee `REFERENCE.md` §3.6
refuses. RRF reads only the *rank* each signal assigned, so there is no scale
to reconcile and no weight to fit:

    score(d) = Σ  1 / (k + rank_i(d))

`k = 60` is the constant from the original paper (Cormack et al., 2009) and is
the value used in both of the owner's own implementations of this — the
Python/pgvector one in Attorney.AI and the T-SQL one in
`dp800-sql-embeddings-vector-search`. It is not tuned here, deliberately: a
constant taken from elsewhere is evidence, and a constant fitted to twelve
labeled postings is an overfit wearing the same digits.

## What this module is not

**It does not change `Match.score`.** `rubric.py` argues that at length and
§15 repeats it: the cosine is what `tests/test_matching.py` validates and what
`Profile.min_match_score` has always compared against, so swapping the ranking
function silently would invalidate both. This is a benchmark variant. Whether
it ever becomes the scorer is a decision with a re-run of the labeled set
beside it, and `docs/ML_EVALUATION.md` sets the bar for that.

**There is no cross-encoder reranker here**, and that is a decision rather
than an omission. The obvious third stage is
`cross-encoder/ms-marco-MiniLM-L-12-v2`, which is free and local and would sit
naturally after the fusion. It needs `torch`, which is absent from this
environment and roughly 800MB installed, so nothing here could measure it —
and an unmeasured ranking stage is precisely the shape CLAUDE.md keeps
recording as a defect: a column, a filter and tests with no way to fire. It is
written down in `docs/ML_EVALUATION.md` as a candidate with its cost attached,
not shipped dark.

## BM25 in this file rather than a dependency

`rank_bm25` is free and MIT, so §3 would permit it. It is thirty lines of
arithmetic and importing it would put the tokenizer boundary in the wrong
place: this scores against `embed.tokenize`, the same function the dense side
uses, so a fusion experiment measures *fusion* rather than two tokenizers
disagreeing about "node.js".

It is also what a production path would run. `apps/api/routers/matches.py`
already filters in Python rather than SQL, because seniority and remoteness
are read out of text; a Postgres `ts_rank_cd` would be a different ranking
function from the one measured here, so the benchmark would not transfer.
Same code both sides or the number means nothing.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from packages.matching.embed import tokenize

#: The rank-fusion constant. From the original RRF paper, and unchanged from
#: the owner's two other implementations of this. Not fitted here — see the
#: module docstring on why a constant borrowed from elsewhere is worth more
#: than one tuned against twelve labeled postings.
RRF_K = 60

#: BM25's term-frequency saturation. Above this, repeating a term stops
#: buying much — which is the property that makes BM25 better than raw tf on
#: a job posting, where a company name recurs and a technology is named once.
#: §15 records that exact asymmetry breaking the ATS scorer.
BM25_K1 = 1.5

#: BM25's length normalisation. 0.75 is the standard value; a posting's length
#: says more about how chatty the employer is than about how relevant it is.
BM25_B = 0.75


@dataclass(frozen=True)
class Ranking:
    """One signal's opinion, as an order rather than as numbers.

    Holding the rank rather than the score is the point of the whole module:
    the caller cannot accidentally add a cosine to a BM25 value.
    """

    name: str
    #: Document key -> 1-based rank. Absent keys were not returned at all.
    ranks: dict[str, int] = field(default_factory=dict)

    @classmethod
    def of(cls, name: str, scored: Sequence[tuple[str, float]]) -> Ranking:
        """Rank by descending score. Ties break on key, so it is reproducible.

        A deterministic tie-break matters more here than it looks: BM25 hands
        out a great many exact zeros on a narrow query, and if their order
        moved between runs the fused result would too.
        """
        ordered = sorted(scored, key=lambda pair: (-pair[1], pair[0]))
        return cls(name=name, ranks={key: i for i, (key, _) in enumerate(ordered, start=1)})


class BM25:
    """Okapi BM25 over a fixed corpus.

    Built once per corpus because the document frequencies and the average
    length are corpus-wide: scoring one posting in isolation is not a thing
    BM25 can do, which is also why this cannot live behind the per-item
    `Variant.score` signature without a corpus handed to it first.
    """

    def __init__(self, documents: Sequence[tuple[str, str]]) -> None:
        self.keys: list[str] = []
        self.tokens: list[list[str]] = []
        for key, text in documents:
            self.keys.append(key)
            self.tokens.append(tokenize(text))

        self.lengths = [len(t) for t in self.tokens]
        total = sum(self.lengths)
        self.avg_length = (total / len(self.lengths)) if self.lengths else 0.0

        containing: Counter[str] = Counter()
        for tokens in self.tokens:
            containing.update(set(tokens))
        self.n_docs = len(self.tokens)

        # Robertson/Sparck-Jones smoothing with Lucene's `+ 1.0` inside the
        # log. That trailing term is what keeps IDF non-negative, and it is
        # worth naming because the obvious belt-and-braces `max(0.0, ...)`
        # around this expression is dead code: `n <= N` makes the quotient
        # non-negative, so the argument is always >= 1 and the log always
        # >= 0. Checked exhaustively for every (N, n) up to 200 — the minimum
        # is 0.0025, at n == N. A clamp that cannot fire reads as protection
        # against a case that does not exist.
        self.idf: dict[str, float] = {
            term: math.log((self.n_docs - count + 0.5) / (count + 0.5) + 1.0)
            for term, count in containing.items()
        }
        self.frequencies: list[Counter[str]] = [Counter(t) for t in self.tokens]

    def scores(self, query: str) -> list[tuple[str, float]]:
        """(key, score) for every document, unsorted."""
        terms = tokenize(query)
        out: list[tuple[str, float]] = []
        for index, key in enumerate(self.keys):
            freq = self.frequencies[index]
            length = self.lengths[index]
            norm = (
                BM25_K1 * (1.0 - BM25_B + BM25_B * (length / self.avg_length))
                if self.avg_length
                else BM25_K1
            )
            total = 0.0
            for term in terms:
                count = freq.get(term, 0)
                if not count:
                    continue
                total += self.idf.get(term, 0.0) * (count * (BM25_K1 + 1.0)) / (count + norm)
            out.append((key, total))
        return out


#: Above this Spearman correlation between two rankings, fusing them is not
#: an experiment. The value is deliberately loose — RRF is worth running on
#: signals that mostly agree, since the wins come from the minority of
#: documents they order differently. It fires for two different reasons, and
#: `agreement_note` tells them apart: the two sides reading the same
#: information, or a corpus so easy that any two scorers order it alike.
DEGENERATE_AGREEMENT = 0.85


def agreement(left: Ranking, right: Ranking) -> float:
    """Spearman rank correlation between two rankings, over their shared keys.

    Exists because a fusion result is uninterpretable without it, and the
    first real run of this module proved why. With `sentence-transformers`
    absent, `embed.get_embedder` falls back to `LexicalEmbedder` — a hashed
    bag of words — so the "dense" side of the fusion was reading the same
    information as BM25. Measured on the twelve adjacent roles: **+0.916**.

    RRF earns its keep by combining signals that fail *differently*. At that
    correlation there is nothing to combine, and the fused ranking scored
    below the dense side alone (NDCG@5 0.452 against 0.577). Reported without
    this number, that reads as "hybrid retrieval does not help on this
    corpus", which is a conclusion about retrieval drawn from an experiment
    that never had two signals in it.

    With the real embedder the same twelve measure **+0.573**, so there the
    fusion row is a measurement. It still lost (0.358 against production's
    0.405); see `docs/ML_EVALUATION.md`.

    Returns 0.0 when the correlation is undefined — fewer than two shared
    keys, or one side ranking everything identically.
    """
    shared = sorted(set(left.ranks) & set(right.ranks))
    n = len(shared)
    if n < 2:
        return 0.0
    a = [left.ranks[key] for key in shared]
    b = [right.ranks[key] for key in shared]
    mean_a, mean_b = sum(a) / n, sum(b) / n
    numerator = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=True))
    spread_a = sum((x - mean_a) ** 2 for x in a)
    spread_b = sum((y - mean_b) ** 2 for y in b)
    if spread_a == 0.0 or spread_b == 0.0:
        return 0.0
    return numerator / math.sqrt(spread_a * spread_b)


def agreement_note(rho: float, *, lexical_embedder: bool) -> str | None:
    """What a reader must be told before trusting a fusion row, or None.

    A high agreement has two causes and they need opposite remedies. With the
    lexical fallback, the "dense" side is a hashed bag of words and the fix is
    the real embedder. With the real embedder already loaded, the corpus is
    the cause: measured with bge-small, the Gate 5 slice agrees at +0.875
    because pastry chefs and truck drivers rank last under any scorer, while
    the adjacent slice agrees at +0.573. Telling that reader to install a
    package they already have sends them to fix the wrong thing.
    """
    if rho < DEGENERATE_AGREEMENT:
        return None
    if lexical_embedder:
        return (
            "NOT A FUSION RESULT. The dense side is LexicalEmbedder, a hashed\n"
            "  bag of words, so both sides read the same information and this\n"
            "  row says nothing about hybrid retrieval. Install the `embeddings`\n"
            "  extra and re-run with --real-embedder."
        )
    return (
        "NOT A FUSION RESULT. Both signals are real and agree anyway: this set\n"
        "  is separated by negatives any scorer ranks last, so the row says\n"
        "  nothing about the close comparisons fusion exists for. Re-run on a\n"
        "  slice that discriminates, e.g. --tag adjacent."
    )


def fuse(rankings: Sequence[Ranking], *, k: int = RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion. Higher is better.

    A document missing from one ranking contributes nothing from that side
    rather than being pushed to last place. Those are different: a lexical
    ranking returns everything, but a vector search with a `TOP_N` does not,
    and treating "not retrieved" as "ranked worst" would let one signal's
    cutoff veto the other signal's first place.
    """
    fused: dict[str, float] = {}
    for ranking in rankings:
        for key, rank in ranking.ranks.items():
            fused[key] = fused.get(key, 0.0) + 1.0 / (k + rank)
    return fused


__all__ = [
    "BM25",
    "BM25_B",
    "BM25_K1",
    "DEGENERATE_AGREEMENT",
    "RRF_K",
    "Ranking",
    "agreement",
    "agreement_note",
    "fuse",
]
