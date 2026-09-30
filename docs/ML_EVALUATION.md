# Measuring the matcher

What the ranking numbers mean, what they are allowed to claim, and what would
have to arrive before any of them could be called production evidence.

Run it:

```bash
make bench-matching
make bench-matching ARGS="--tag adjacent --k 5"
make bench-matching ARGS="--holdout 0.3 --seed 0 --json /tmp/run.json"
```

---

## Why this exists

CLAUDE.md Gate 5 asks that "the ones you'd actually apply rank in the top 10",
and `tests/test_matching.py` checks exactly that. It is a real gate and it is
nearly blind:

- It cannot see a wanted posting slide from rank 1 to rank 10.
- It cannot compare two scorers that both pass.
- It reports one bit where the question — is this ranking better than that
  one — needs a number and an interval around it.

So there was no way to answer "did that change help", which makes every other
improvement to the matcher unfalsifiable.

| Piece | Where |
|---|---|
| Ranking and classification metrics | `packages/matching/metrics.py` |
| Labeled corpus, provenance, leak-safe splitting | `packages/matching/labels.py` |
| Variant comparison, experiment records | `packages/matching/benchmark.py` |
| The labels themselves | `seeds/labeled_matches.yaml` |
| CLI | `scripts/bench_matching.py` |
| Tests | `tests/test_matching_metrics.py` |

No new dependency. The metrics are arithmetic, and a reader should be able to
check them by hand against the worked examples in the test file — which is
where every expected value came from, rather than from the implementation.

---

## The result, as of this writing

32 labeled postings, one synthetic backend profile, lexical embedder:

```
variant                  ndcg@10            95% CI     P@k     MAP     ROC  ms/item
production+seniority       0.978       [0.93,1.00]   1.000   0.982   0.972    0.325
production                 0.965       [0.87,1.00]   0.900   0.938   0.929    0.791
body_only                  0.957       [0.86,1.00]   0.900   0.935   0.931    0.111
jaccard                    0.942       [0.85,1.00]   0.900   0.934   0.927    0.013
title_only                 0.715       [0.42,0.91]   0.800   0.858   0.875    0.109
constant                   0.000       [0.00,0.01]   0.000   0.372   0.500    0.001

leader              : production+seniority
statistically tied  : production, body_only, jaccard
production candidate: NO
```

Four things in that table are worth more than the ordering.

**The shipped scorer is not distinguishable from token overlap.** `jaccard` is
five lines — the Jaccard index of the token sets — and it sits inside the
leader's confidence interval at **1/60th of the latency**. On this data the
embedding, the 0.35/0.65 weighting, and the role-alias floor have not been
shown to buy anything. That is not a claim that they are useless; it is a
claim that 32 synthetic labels cannot see the difference, which is a different
and more honest statement than the one the table's ordering suggests.

**The original Gate 5 labels measure nothing about ranking.** Restricted to
the twenty postings the gate uses, `production` and `jaccard` both score a
perfect 1.000. The negatives are pastry chefs, truck drivers and baristas; any
scorer separates those from a backend role. `test_the_original_gate5_labels_cannot_tell_the_variants_apart`
asserts this so it stays visible.

**On adjacent roles everything collapses.** Restricted to the twelve roles
that share vocabulary with the profile and differ on something real —
`Engineering Manager, Backend` (every technology matches, the job is
management), `Senior Frontend Engineer` (title matches almost perfectly, no
overlap in the work), `Backend Engineer (Go)` (right job, wrong language) —
NDCG@5 drops to **0.577**, and the constant control is statistically tied with
everything. This is where the matcher's real weakness lives, and it was
invisible before there were hard negatives to expose it.

**The seniority filter is off by default and costs precision.** `filters.seniority_ok`
returns `True` whenever `target_seniority` is unset. The consequence is
measurable: `Junior Backend Engineer` — a perfect technology match at the wrong
level — ranks in the top ten, and P@10 goes from 0.900 to 1.000 when the target
is armed. Deciding whether to arm it by default is a separate change; this is
the number to decide it against.

This used to add "and no production caller sets one", which was true of the code
and is no longer true of the product: `apply_filters` reads the profile's own
rung, and `/profile` has the control it shipped without. Unset is still the
default, so the number above is still the number — what changed is that arming
it no longer takes curl.

**The same is now true of a years demand, and it has no number yet.**
`filters.experience_ok` excludes a posting whose *mandatory* experience
requirement exceeds `profiles.max_required_experience_years`
(`matching/experience.py`). It is unmeasured here on purpose: the Gate 5
postings are fixtures and none of them states a years requirement, so running
the benchmark with a bound armed would report a difference of exactly zero and
invite the reading that the filter does nothing. The twelve crawled postings in
`tests/fixtures/golden/` do state them — 7 of 12 — but carry no relevance
labels, which is P1 again.

---

## Rules the harness enforces in code

**Ties are broken against the scorer.** A scorer returning the same number for
everything produces one tie group; under a stable sort its NDCG is whatever
the input order happened to be, so a null model that has learned nothing can
report a perfect ranking. `TieBreak.PESSIMISTIC` puts the less relevant item
first, and it is the default and the reported number. The control's 0.000
above is that rule working.

**The control is a row in the table.** `constant` returns 0.5 for everything.
Any variant that cannot clear it by more than the bootstrap interval has not
been shown to rank. Most benchmark tables have no row like this.

**A verdict names what it cannot claim.** `summarize()` returns
`production_candidate: False` and lists blockers. Today: the labels are
fixture-only, and 32 items is below the ~100 needed for differences this size
to survive resampling. `test_fixture_only_labels_block_a_production_claim`
holds it there — no run over synthetic labels may report a production
candidate, however good the numbers look.

**A variant never sees the grade.** The scoring callable takes the profile
text and a posting. `test_a_variant_never_sees_the_relevance_label` flips a
label and asserts the score does not move — the master spec's §45 objection to
self-grading, checked rather than promised.

**Splits group by company.** Two postings from one employer share
boilerplate, so splitting them across train and holdout lets a model recognise
the company instead of the job. `split()` groups first, deterministically on
the group name, so adding a posting does not reshuffle everything before it.

**Provenance is required, never defaulted.** A missing `provenance` field
raises. The safe default would be `fixture`, so a set of real owner labels
that lost the field in an edit would quietly understate itself — the one
direction of error that makes real numbers look synthetic.

---

## What is deliberately not here

**No trained ranker.** No XGBoost, no LambdaMART, no cross-encoder. The master
spec asks for all of them benchmarked, and benchmarking them honestly needs
labels this repo does not have: 32 fixture-graded postings for a single
profile cannot fit a learning-to-rank model without memorising them, and a
model fitted on them would report a number that means nothing. The spec's own
§66 says what to do in this situation — build the pipeline, the baselines and
the evaluation, and state what data is missing. That is what this is.

The harness is model-agnostic on purpose: a trained ranker becomes a `Variant`
with a `score` callable and gets the same table, the same control, and the
same refusal to overclaim.

**No calibration in production.** `expected_calibration_error` and
`brier_score` exist and nothing calls them yet. They matter because
`Profile.min_match_score` compares a raw cosine against a threshold, so the
units have to mean something — but calibrating against fixture labels would
fit the fixtures. It waits for the same real labels everything else does.

---

## What would have to arrive

In rough order of how much each would buy:

1. **~100 owner-labeled real postings.** The single blocker on every claim
   here. A crawl already produces the postings; grading them 0-3 in
   `seeds/labeled_matches.yaml` with `provenance: owner` is the work. At that
   point the fixture-only blocker clears and the intervals narrow enough to
   separate variants that are currently tied.
2. **Hard negatives from the owner's own feed.** The twelve adjacent roles
   here were written, not observed. Real ones — the postings the owner scrolled
   past — are better, and they are free: `provenance: feedback`, derived from
   decisions the feed already asks for.
3. **A second profile.** Every number above describes one synthetic backend
   profile. Nothing has been shown to generalise across profiles because there
   is only one.
4. **Outcome labels.** Interview conversion is the metric the system actually
   exists to move. It is also the noisiest and the slowest, and per the spec's
   §37 a rejection is not proof of a bad match.

Until (1), treat everything in this document as a regression signal: it will
tell you when a change made the matcher worse, and it will not tell you that
the matcher is good.

---

## Hybrid retrieval, and the experiment this machine could not finish

`packages/matching/hybrid.py` adds Reciprocal Rank Fusion of the dense cosine
with BM25, as two benchmark variants (`hybrid_rrf`, `bm25_only`). It exists
because this document already names where the matcher is weak and the weakness
has the shape a lexical signal is supposed to fix: on the twelve adjacent
roles NDCG@5 is 0.577 with the constant control statistically tied against
everything, and adjacent roles differ from each other in a handful of tokens
that an embedding is *designed* to collapse.

**The result, run here on 2026-09-30:**

```text
variant                   ndcg@5            95% CI     P@k     MAP     ROC
production+seniority       0.774       [0.47,1.00]   1.000   0.959   0.906
jaccard                    0.599       [0.17,0.96]   0.800   0.701   0.578
production                 0.577       [0.16,1.00]   0.800   0.739   0.656
hybrid_rrf                 0.452       [0.15,0.94]   0.800   0.718   0.594
bm25_only                  0.452       [0.13,0.91]   0.800   0.685   0.500
constant                   0.062       [0.00,0.45]   0.200   0.490   0.500

hybrid_rrf: dense/lexical rank agreement rho=+0.916
```

**That row is not a verdict on hybrid retrieval, and the last line is why.**
RRF combines signals that fail differently. `sentence-transformers` is not
installed here, so `embed.get_embedder` falls back to `LexicalEmbedder` — a
hashed bag of words — and the "dense" side of the fusion was reading the same
information as BM25. Spearman **+0.916**. The experiment had one signal in it.

Two things follow, and the second is the reason this section exists at all:

- `hybrid_rrf` scoring level with `bm25_only` to three decimals is the
  ablation doing its job. It was put there to detect exactly this.
- **A number that cannot be interpreted is more dangerous than no number.**
  Reported without the correlation, 0.452 reads as "hybrid retrieval was tried
  and lost", which would close the question on the strength of a run that
  never tested it. `hybrid.agreement` is computed on every fusion pass,
  recorded in the experiment record so it survives into `--json`, and printed
  under the table with the refusal spelled out.

**Finishing it needs the owner's machine.** `BAAI/bge-small-en-v1.5` downloads
from `huggingface.co`, which this environment's network policy denies at
CONNECT with a 403 — the same wall `make validate-seeds` and the live gates
hit. On a machine with the `embeddings` extra installed and the model cached:

```bash
make bench-matching ARGS="--tag adjacent --k 5 --real-embedder"
```

Read `rho` first. Below ~0.85 the fusion row is a real measurement; at 0.9 and
above it is still the degenerate case and the NDCG means nothing.

**What would still be true if it wins.** Nothing promotes on twelve
fixture-grade labels. The verdict block already refuses, for both reasons it
always gives, and a win here would move the question to the labeling loop
rather than to the scorer.

### The reranker is deliberately not built

The natural third stage is a cross-encoder — `cross-encoder/ms-marco-MiniLM-L-12-v2`
is free, local, CPU-runnable, and jointly encodes (query, passage) rather than
comparing two independent vectors, which is precisely the comparison a cosine
is worst at. It is not here because it needs `torch`, nothing in this
environment can download the weights, and an unmeasured ranking stage is the
shape this repo keeps finding as a defect: a column, a filter and tests with
no way to fire. It is recorded as a candidate with its cost attached rather
than shipped dark.

### Why BM25 is in the tree rather than a dependency

`rank_bm25` is MIT and §3 would permit it. Two reasons not to:

- It would tokenize differently. `hybrid.BM25` scores through
  `embed.tokenize`, the same function the dense side uses, so a fusion
  experiment measures fusion rather than two tokenizers disagreeing about
  `node.js`.
- **It is what a production path would run.** The obvious lexical source is
  Postgres full-text search, which this repo already has a server for — but
  `ts_rank_cd` is a different ranking function from the one measured here, so
  a benchmark won on BM25 would not transfer to a feed served by FTS.
  `apps/api/routers/matches.py` already filters in Python rather than SQL, for
  the same reason it always has: the fields are read out of text. Same code
  both sides, or the number means nothing.
