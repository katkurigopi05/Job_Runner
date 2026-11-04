# Qwen3-Embedding-4B: file validation and ranking-weight report
Measured 2026-11-03. Code: `155d78946f43e98ecf6d08ccdc941f8fbd55824f`.

**Result:** the embedding file is structurally valid, finite and unit-normalized. Combining keywords with Qwen retrieves more known targets than either alone. No weight is established as a production winner. The development-selected 30% keyword / 70% Qwen blend gains paraphrased targets but regresses some natural questions; its validation improvement over 50/50 is not statistically clear.

These are **ranking-fusion weights**, not neural-network parameters. The `.npy` contains document embeddings, not a trainable model checkpoint. This report does not change the assistant, its settings, or model weights.

## File and dataset

- File: `/Users/gopikrishnareddykatkuri/Desktop/Job_Runner/storage/embed_rank_bench/whole_index_2026-11-02/vectors.nosync/qwen4b_full_f16.npy`.
- Shape: **224,361 × 2,560**, float16, **1,148,728,448 bytes** (1.15 GB decimal).
- Matching export: `whole_index.json.gz`, dated 2026-11-02; **16,306 postings**, including **55 benchmark-only postings** that were closed when exported. This is a fixed public-text snapshot, not today’s live corpus.
- All 56 known targets are present; 112 questions comprise 56 natural and 56 paraphrased forms.
- All vector elements are finite; **zero zero-length vectors**. Norm range: **0.99975497–1.00024354**; mean 0.99999988.
- Unique posting IDs, chunk count, and every 500-character / 25-character overlap were validated. Descriptions were reconstructed without duplicating overlap text.
- Cached question vectors are from local `Qwen3-Embedding-4B-GGUF:Q4_K_M`, not the TPU BF16 query encoder. All 112 question/vector mappings were checked against the earlier named query artifact; maximum vector difference was 7.45e-09.
- Structural checks do not independently prove each embedding was generated from its claimed text. No document was re-embedded in this run.

## Method fixed before selection

1. Compute exhaustive cosine similarity over every chunk, in float32; each posting receives its best chunk score. No approximate shortlist is used.
2. Use the repository’s BM25 implementation on title + company + reconstructed description. Only positive BM25 scores receive a keyword rank.
3. Compare eleven Qwen weights, 0.0 through 1.0 in steps of 0.1, with RRF constant **k = 60**:

```text
score = (1 − α)/(60 + keyword_rank) + α/(60 + Qwen_rank)
```

A missing keyword rank contributes zero. Ties are broken by posting UUID, so a top-five list has exactly five entries. Weights combine ranks, not incomparable raw BM25 and cosine scores.

4. Split by target UUID: SHA-256 first byte × 100 // 256 < 30 is validation. Both question forms remain together: **37 development targets and 19 validation targets**.
5. Select by development mean reciprocal rank (MRR), averaging natural and paraphrased questions equally; break ties toward 50/50, then lower Qwen weight. The selected α is **0.7**.

**Validation caveat:** these 56 targets were inspected in earlier repository experiments. The split prevents tuning on validation within this run, but it is not a pristine held-out dataset.

## All 112 questions: descriptive sweep

Counts are targets retrieved in the first five or twenty, out of 56 per form. Higher MRR is better. The all-question table was not used to select the weight.

| Keyword / Qwen | Natural top 5 | Paraphrased top 5 | Natural top 20 | Paraphrased top 20 | Natural MRR | Paraphrased MRR |
|---|---:|---:|---:|---:|---:|---:|
| 100 / 0 | 36/56 | 4/56 | 48/56 | 10/56 | 0.498 | 0.050 |
| 90 / 10 | 41/56 | 4/56 | 48/56 | 10/56 | 0.571 | 0.066 |
| 80 / 20 | 42/56 | 4/56 | 48/56 | 11/56 | 0.605 | 0.062 |
| 70 / 30 | 41/56 | 8/56 | 50/56 | 12/56 | 0.633 | 0.079 |
| 60 / 40 | 44/56 | 8/56 | 51/56 | 12/56 | 0.653 | 0.093 |
| 50 / 50 | 45/56 | 9/56 | 50/56 | 18/56 | 0.661 | 0.110 |
| 40 / 60 | 45/56 | 11/56 | 50/56 | 20/56 | 0.649 | 0.127 |
| 30 / 70 | 42/56 | 12/56 | 49/56 | 19/56 | 0.638 | 0.140 |
| 20 / 80 | 36/56 | 13/56 | 47/56 | 20/56 | 0.618 | 0.147 |
| 10 / 90 | 36/56 | 12/56 | 46/56 | 21/56 | 0.575 | 0.128 |
| 0 / 100 | 33/56 | 11/56 | 44/56 | 20/56 | 0.546 | 0.129 |

**Tradeoff:** 40/60 preserves the 50/50 blend’s 45 natural top-five hits and increases paraphrased hits from 9 to 11. The development-selected 30/70 yields 42 natural and 12 paraphrased hits. An 80% Qwen weight reaches 13 paraphrased hits but reduces natural hits to 36. Maximizing one column would hide this tradeoff.

## Validation of the development-selected 30/70 weight

| Keyword / Qwen | Natural top 5 | Paraphrased top 5 | Natural MRR | Paraphrased MRR |
|---|---:|---:|---:|---:|
| 100 / 0 | 12/19 | 1/19 | 0.540 | 0.034 |
| 50 / 50 | 15/19 | 4/19 | 0.704 | 0.157 |
| 30 / 70 | 15/19 | 5/19 | 0.693 | 0.178 |
| 0 / 100 | 11/19 | 4/19 | 0.547 | 0.138 |

Against 50/50, the selected weight’s validation mean-MRR gain is **+0.0052**, with paired-target bootstrap 95% interval **[-0.0068, +0.0193]** (10,000 resamples, fixed seed). The interval includes zero. Its development gain was only +0.0023. There is no reliable evidence here that tuning away from equal weights improves overall quality.

On validation target ranks versus 50/50: natural **0 wins / 4 losses / 15 ties**; paraphrased **13 wins / 3 losses / 3 ties**. Natural top-five membership is unchanged; one paraphrased target enters the top five. These are rank changes for the known target, not relevance grades for other results.

## Concrete gains and regressions

Top-five boundary changes for 30/70 versus 50/50 across all questions:

| Form / split | Question | 50/50 rank | 30/70 rank |
|---|---|---:|---:|
| paraphrased / validation | Seeking jobs focused on operating machinery to gather information for improving automated systems | 20 | 4 |
| natural / dev | What Regional Sales Director roles focus on leading teams for national security accounts? | 5 | 7 |
| paraphrased / dev | jobs creating data models and reports to support business decisions | 3 | 9 |
| paraphrased / dev | jobs leading teams that design and improve user onboarding and satisfaction for prospective clients | 7 | 4 |
| paraphrased / dev | jobs overseeing efforts to streamline or automate hiring processes while improving candidate experience | 6 | 3 |
| natural / dev | What jobs exist for a Software Engineer focused on safety processing systems? | 1 | 18 |
| paraphrased / dev | Looking for jobs that oversee agreement negotiations and provide legal guidance for business growth initiatives | 2 | 6 |
| paraphrased / dev | jobs assisting users with technical issues and maintaining help resources | 12 | 5 |
| paraphrased / dev | jobs shaping how automated exchanges feel and sound for business users | 12 | 3 |
| natural / dev | What Family Nurse Practitioner or Physician Assistant roles are available in Laurel Heights? | 4 | 6 |

## Why the Qwen-only count differs from the earlier report

The prior report gave 34 natural top-five hits; this run gives **33** with deterministic UUID tie-breaking. The AI Research Engineer target at LanceDB shares its exact best chunk with Senior Documentation Engineer and Senior System Test Engineer: the same opening company introduction. Giving every tied posting the optimistic rank 4 reproduces the earlier **34 natural / 11 paraphrased** counts. A real five-item list cannot put all three in the two remaining slots; UUID tie-breaking places the target sixth. This is a tie-accounting difference, not file corruption. It also illustrates how shared company boilerplate can dominate best-chunk similarity.

## Verification and timing

- Six synthetic checks passed: deterministic ties, missing lexical scores, endpoint weights, equal-weight parity with the repository’s RRF, verified overlap reconstruction, invalid-weight rejection, and stable target splitting (some checks share a test). They make no database, model or network calls.
- An independent float64 calculation for five postings across all 112 questions agrees with the batched results within **1.08e-06** (tolerance 2e-06).
- Source files are opened read-only; NumPy loading uses `allow_pickle=False`. SHA-256 fingerprints are recorded below and in the JSON.
- This final offline run took **13.76 s**. The shared batched cosine pass for all 112 cached queries took **0.70 s**; median BM25 scoring was **25.7 ms/query**, and rank fusion **1.7 ms/blend**.
- These are cached, batched offline timings, **not assistant response latency**. No query model load, live SQL, metadata filtering, excerpt creation or answer generation is included.

## Recommendation and limits

**Keep the assistant’s current settings and ordering unchanged.** For this experimental raw-rank fusion, 50/50 is the defensible baseline; 40/60 is a promising candidate for a fresh graded comparison because it retains natural top-five recall while improving paraphrases. Do not declare 40/60 the validated winner after inspecting the all-question table. The preselected 30/70 candidate did not establish a statistically clear overall advantage over 50/50 and has visible natural-query regressions.

The application currently uses Qwen for discovery and retains bge-small ordering of keyword matches; this experiment does not reproduce that pipeline or expose a new configurable production weight. It omits company, area, level, title priority, rare-word abstention, and other assistant constraints. No comparison here authorizes removing those rules.

Each question has only one known target. Five other relevant jobs still count as a miss, and **semantic finds beyond that target remain ungraded**. This run does not use or expand the owner’s relevance-grade pool, report nDCG/precision, or establish a promotion decision. A next promotion study needs fresh questions, pooled relevance judgments and the real filtered `retrieve()` path. No live database was read or written, no model was loaded, and no running checkout was changed.

## Artifacts and reproduction

- [Per-question ranks for every weight](qwen-weights-2026-11-03.csv).
- [Metrics, fingerprints and bootstrap comparisons](qwen-weights-2026-11-03.json).
- [Independent numeric verification](qwen-weights-verification-2026-11-03.json).
- Local reproducible harness: `/private/tmp/jobrunner-qwen-weight-study/weights.py`, `test_weights.py`, and `query_manifest.json`. Scratch scripts remain outside the repository.

```sh
cd /private/tmp/jobrunner-wt-qwen-weights
PYTHONPATH=. .venv/bin/python /private/tmp/jobrunner-qwen-weight-study/test_weights.py
OPENBLAS_NUM_THREADS=2 VECLIB_MAXIMUM_THREADS=2 OMP_NUM_THREADS=2 PYTHONPATH=. .venv/bin/python /private/tmp/jobrunner-qwen-weight-study/weights.py
```

| Input | SHA-256 |
|---|---|
| qwen4b_full_f16.npy | `30001ecec51bab5f5674c88e1e8798eff09f3c4485327b5ade8e1abccbc41511` |
| ollama_q4_questions.npy | `45c764c6a81e4a9c26862578ffbe2bd7099565d48f1eb28caa24d279a1ce4795` |
| whole_index.json.gz | `8f7ea38dcd860c03517161a35daf96030a22497b763c29cb7c84f92717576343` |
