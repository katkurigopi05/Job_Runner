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
returns `True` whenever `target_seniority` is unset, and no production caller
sets one. The consequence is measurable: `Junior Backend Engineer` — a perfect
technology match at the wrong level — ranks in the top ten, and P@10 goes from
0.900 to 1.000 when the target is armed. Deciding whether to arm it by default
is a separate change; this is the number to decide it against.

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
