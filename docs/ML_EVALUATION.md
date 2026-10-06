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

**That 0.577 is the plain lexical embedder's number, and neither benchmark run
is the feed's.** `bench_matching` scores with `LexicalEmbedder` unless
`--real-embedder` is passed. The `embedder:` line under the header says so, and
the figure above was quoted without it. With bge-small the same twelve give
`production` **0.405**. The run is in the hybrid section below.

This paragraph first called bge-small "the embedder the feed actually uses",
because the owner's `.env` names it. It is not. Once corpus statistics exist,
as they have since 2026-09-18, `incremental.run_matching_pass` scores with
`LexicalEmbedder(frequencies=…)`, the IDF-weighted `lexical-idf@2`, whatever
`EMBEDDING_BACKEND` says. bge-small is reached only while the corpus is too
small to weight. The benchmark runs neither that embedder nor those weights, so
the feed's own number on these twelve is unmeasured. Of the two that were
measured, plain lexical (0.577) is the closer relative: the same tokenizer and
hashing, without the weights.

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

### Finished on the owner's machine, 2026-09-30

bge-small cached, `EMBEDDING_BACKEND=sentence-transformers`, same labeled set
(`f29a1c8be0ec5700`). The numbers reproduce exactly across runs:

```text
--tag adjacent --k 5 --real-embedder
variant                   ndcg@5            95% CI     P@k     MAP     ROC
jaccard                    0.599       [0.17,0.96]   0.800   0.701   0.578
production+seniority       0.597       [0.28,1.00]   0.800   0.876   0.812
bm25_only                  0.452       [0.13,0.91]   0.800   0.685   0.500
body_only                  0.436       [0.08,1.00]   0.600   0.645   0.469
production                 0.405       [0.10,0.95]   0.600   0.684   0.562
hybrid_rrf                 0.358       [0.06,0.85]   0.600   0.654   0.516
title_only                 0.263       [0.00,0.87]   0.400   0.724   0.438
constant                   0.062       [0.00,0.45]   0.200   0.490   0.500

hybrid_rrf: dense/lexical rank agreement rho=+0.573
```

**This time the row is a measurement, and fusion lost.** At +0.573 the two
signals order the adjacent roles differently, which is the precondition RRF
needs, and the fused ranking still came in below both of its inputs. The
mechanism shows in the top five. The fusion carries the same two false
positives as `production`: `adj-junior-backend` and `adj-eng-manager`. BM25
alone kept the manager out, and fusion let it back in.

What it does not say: every row except the control sits inside every other
row's interval, and the verdict block still names no candidate for both of its
usual reasons. The whole claim is that fusion did not help on twelve
fixture-grade labels. Hybrid retrieval stays a benchmark variant and
`Match.score` is unchanged.

Three things this run found that the first one could not:

- **The recorded 0.577 was the lexical fallback**, as noted under the adjacent
  roles above. On bge-small, `production` is 0.405 on the adjacent slice
  (lexical 0.577) and 0.992 at NDCG@10 on Gate 5 (lexical 1.000). The hashed
  bag of words beating bge-small here fits the pattern CLAUDE.md §15 keeps
  recording. These fixtures were written beside keyword-matching code, so they
  reward keyword overlap. That is not evidence that bge-small is the worse
  embedder, and nothing here separates the two.
- **The degenerate check fired with the real embedder loaded, and its advice
  was wrong.** With bge-small, agreement is **+0.875** on the Gate 5 slice and
  +0.911 on all 32, because pastry chefs rank last under any scorer. The
  message then told the reader to install the `embeddings` extra, which was
  already installed. `hybrid.agreement_note` now tells the two causes apart.
  The lexical fallback is told to install the extra. A real embedder is told
  that the slice cannot discriminate and pointed at `--tag adjacent`.
- **Arming seniority is the one change ahead under both embedders**: 0.597
  against 0.405 on bge-small, and 0.774 against 0.577 on lexical. It drops
  `adj-junior-backend` from the top five, which is the P@10 finding above seen
  from a harder slice. The lead is still inside the interval. It points at the
  `target_seniority` default decision rather than at the scorer.

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

## Embedding models for the assistant's search, and the re-ranker (2026-10-05)

This is about the assistant's chunk search (`matching/retrieve.py`), not the
feed's score. *The reranker is deliberately not built*, above, is about the
feed and still stands: nothing here touches `Match.score`.

### What was measured

56 open in-area postings were drawn at random. Nemotron (OpenRouter) wrote two
questions for each: a **natural** one that may use the posting's words but not
its company, and a **paraphrased** one that may not reuse the title or any
technology, product or company name. Each model embedded the same chunk texts
(the stored 500-character chunks) and ranked the pool by each posting's best
chunk. The score is the rank of the posting a question was written from.

All four models ran on the M4's GPU (MPS; Qwen 4B through Ollama).

| pool | | bge-small | bge-large | Qwen3-Embedding-0.6B | Qwen3-Embedding-4B (Q4) |
|---|---|---|---|---|---|
| 1,000 postings, 14,112 chunks, 1.35M tokens | natural, top 5 | 64% | 71% | 80% | not run |
| | paraphrased, top 5 | 11% | 29% | 34% | not run |
| 400 postings, 5,652 chunks, 543K tokens | natural, top 5 | 77% | 79% | 93% | 91% |
| | paraphrased, top 5 | 32% | 45% | 52% | 71% |

Paired differences in MRR, 95% bootstrap interval over questions:

| comparison | natural | paraphrased |
|---|---|---|
| bge-large over bge-small (1,000) | +0.078 [+0.019, +0.147] | +0.088 [+0.036, +0.149] |
| Qwen 0.6B over bge-large (1,000) | +0.081 [+0.013, +0.150] | +0.062 [-0.009, +0.138] |
| Qwen 4B over Qwen 0.6B (400) | +0.046 [-0.036, +0.132] | +0.114 [+0.019, +0.212] |

So the order is Qwen 4B, Qwen 0.6B, bge-large, bge-small. Two of the steps are
not established: 0.6B over bge-large on paraphrased questions, and 4B over
0.6B on natural ones.

### What each costs on the owner's machine

| | bge-small | bge-large | Qwen 0.6B | Qwen 4B (Q4) |
|---|---|---|---|---|
| chunks per second | 366 | 34 | 19 | 3.3 |
| the corpus: 141,603 chunks, 13.6M tokens | 6 min | 69 min | 2 hours | 12 hours |
| dimensions | 384 | 1,024 | 1,024 | 2,560 |
| stored as `halfvec` | 109 MB | 290 MB | 290 MB | 725 MB |

Three things learned running them:

- **Time a model on distinct texts.** A probe that sent one sentence 64 times
  reported 29 chunks a second for Qwen 4B. On real chunks it does 3.3.
- **Cap Ollama's context.** Qwen 4B loaded at Ollama's default 32,768-token
  context took 8.7 GB of 16; at 1,024 it took 3.3 GB.
- **Half precision did not help here.** Qwen 0.6B in fp16 on MPS was no faster
  than fp32 and peaked at 2.7-3.1 GB against 0.85 GB. Batches of 8 were
  fastest (18.7 chunks a second, against 17.0 at 32 and 15.5 at 64).

Qwen's vectors can be shortened. Qwen 4B's first 384 numbers, re-normalised,
still put the right posting in the top five for 55% of paraphrased questions
on the 400 pool, above bge-large's 45% at 1,024. Shortening saves storage and
search time, not embedding time.

### The re-ranker

Re-embedding the corpus is the expensive way to use a better model. The cheap
way keeps bge-small and has the better model re-score only the best chunk of
the top candidates. Simulated from the saved vectors, 1,000 pool, top 5:

| | natural | paraphrased | one-off cost |
|---|---|---|---|
| bge-small alone | 64% | 11% | none |
| bge-small, then Qwen 0.6B on the top 30 | 77% | 36% | none |
| bge-small, then Qwen 0.6B on the top 100 | 71% | 29% | none |
| Qwen 0.6B for everything | 80% | 34% | 2 hours |

That is what `matching/rerank.py` does, with `CHAT_RERANK_MODEL` naming the
model. On the 400 pool Qwen 4B as the re-ranker reached 84% and 52%: it cannot
find what bge-small's top 30 left out, and at 3.3 chunks a second it costs
about 9 s a question against 1.6 s.

Measured on the live database (10,589 open postings) with the real model: a
question went from 0.4-1.0 s to 1.7-2.3 s, and free memory from 58% to 42%
with both models loaded.

### The re-ranker, measured live: it did not help

The table above is a simulation, and the last section warned that the shipped
path differs from it. On 2026-10-05 the same 56 postings and 112 questions
were run through the assistant's real `retrieve()`, over all 10,589 open
postings (141,603 chunks, 13.6M tokens), once with the re-ranker patched out
and once with Qwen3-Embedding-0.6B on the top 30. All 56 postings are inside
the search area. The score is where the posting a question was written from
lands among the five shown to the model and the list under the answer.

| | natural, off | natural, on | paraphrased, off | paraphrased, on |
|---|---|---|---|---|
| first | 50% | 48% | 4% | 2% |
| in the five shown | 70% | 64% | 7% | 7% |
| in the first 15 | 82% | 79% | 12% | 14% |
| anywhere in the list | 86% | 86% | 16% | 16% |
| MRR | 0.602 | 0.568 | 0.054 | 0.045 |
| median time | 0.52 s | 2.13 s | 0.81 s | 2.36 s |

Per question, the re-ranker moved the right posting up on 4 natural questions
and down on 11, and up on 4 paraphrased ones and down on 5. The rest did not
move.

Why the simulation did not carry over:

- **It re-ranked a different first stage.** The simulation's top 30 came from
  bge-small vectors alone. The shipped top 30 is chosen by keywords and
  ordered by keyword score, with a title match counted twice, then fused with
  vector rank. That order is already good when the question names the job,
  and the re-ranker throws it away: "What Primary Care Nurse Practitioner jobs
  are available for adult patient care?" went from rank 2 to 21.
- **A re-orderer cannot add what was not found.** For 47 of the 56 paraphrased
  questions the posting never entered the list, because the question shares no
  distinguishing word with it. "Anywhere in the list" is identical with the
  re-ranker on and off, as it has to be.

What this establishes and does not: 70% against 64% is three questions of 56,
so the re-ranker being *worse* is not shown. That it is *no better*, for 1.6 s
a question and a second model in memory, is. It is off on the owner's machine.
8 of the 56 natural questions were answered by the role path (CLAUDE.md §22),
which finds postings by title and was not part of the simulation at all.

The paraphrased row is the real gap, and it is the one CLAUDE.md §20 already
measured from the other side: letting vectors add candidates reaches
paraphrases and brings near-topic noise with it. That is a change to which
postings are found, not to their order.

The script is `storage/embed_rank_bench/er_live.py`; it makes no LLM call.

### What this does not establish

- 56 postings, and one free model wrote every question. Enough to order four
  models; the percentages will move on the owner's own questions.
- The pools are 400 and 1,000 postings against 10,589 live, so every
  percentage here is higher than the assistant's would be.
- The re-ranking rows are simulated from stored vectors. The shipped path also
  fuses keyword rank and applies the search area before the re-ranker runs.
  This is the caveat that mattered: see *The re-ranker, measured live*, above.
- A second check, each model's top five on 8 open questions graded by an LLM,
  has not run: the OpenRouter allowance was spent.

The benchmark scripts and vectors are in `storage/embed_rank_bench/`, which
git ignores. Committing it as a repeatable command is still to do.
