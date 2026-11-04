# Qwen3-Embedding-4B ranking weights, judged by Qwen 27B

Measured 2026-11-03 (America/Los_Angeles; grades stamped 2026-11-04 UTC). Builds on
[`qwen-weights-2026-11-03.md`](qwen-weights-2026-11-03.md), which ranked the same 112 questions at
eleven keyword/Qwen blends and scored only where each question's one known posting landed. This
report grades **everything those blends put in their top five**.

**Result.** Leaning on keywords is clearly worse: every Qwen weight from 0.0 to 0.4 scores below the
0.5 baseline on both halves, with every interval below zero. From 0.5 to 0.9 the blends are close.
**0.6 is the dev-selected weight** (best dev nDCG@5, 0.762 against 0.737) and its gain over 0.5
holds on the validation half, small: nDCG@5 **+0.013** (95% interval **+0.001 to +0.026**), with no
change in how many of the five are relevant. Pure Qwen (1.0) is no better than 0.5.

**Recommendation.** 0.6 over 0.5 is a defensible, small improvement in the ordering of the top
five, not in their relevance. It is measured on an offline blend, not on the assistant's search
(see Limits), so it does not by itself move any production setting.

These are **model judgments**, not the owner's grades. They are stored apart from
`retrieval_judgments` and must not be read as human labels.

## The judge

| | |
|---|---|
| Model | `unsloth/Qwen3.8-27B-GGUF:UD-Q6_K`, SHA-256 `c9c206812fbe4ac7b76a729e25928b63f2ae89d37f69da7a71c20aec763cd436` |
| Where | The owner's Kaggle session, two Tesla T4s, 4,096-token context, thinking off, temperature 0 |
| Loaded from | Private dataset `spark0505/qwen38-27b-q6-private/versions/1`, startup script SHA-256 `cce702da…4fc1`; Qwen3 8B unloaded first |
| Rubric | 0 irrelevant or contradicts the question, 1 plausible partial, 2 directly meets it; SHA-256 `b5b13e8b…39c6` |
| Prompt | The question and full public posting text (title, company, description), blind: no weight, rank or target. One or two candidates a call, never compared |
| Calls | 1,286 (4 pilot), median 8.6 s, about 3.3 hours |
| Privacy | Public posting text only, sent through the normal `PrivacyProvider` masking; every call in the run's own audit trail |

The owner's daily remote-call ceiling (`LLM_DAILY_REMOTE_CALLS`, 500) stopped the run at 460
calls. The owner asked for the evaluation to finish the same day, so the ceiling was raised for the
grading process only; `.env` and the assistant's own limit were not changed.

## Coverage and reliability

- **All 1,421 pool pairs graded.** No answer failed to parse, none was too long for the context,
  and no posting was trimmed. 336 pairs were graded two to a call, 1,085 alone.
- **Negative controls: 16 of 16 graded 0.** One unrelated posting from another question was hidden
  among the candidates of eight dev and eight validation questions.
- **Repeats: 15 of 16 identical.** Pairs first graded beside another posting were graded again
  alone.
- **Known targets: 63 of 66 graded 2, 3 graded 1.** The posting each question was written from.
- Grades overall: 514 at 0, 443 at 1, 464 at 2.

## Results

P@5 counts grades 1 and 2, direct@5 counts grade 2, and nDCG@5 is normalised against the best five
grades in that question's whole pool. Every question is complete at every weight.

| Qwen weight | Dev P@5 | Dev direct@5 | Dev nDCG@5 | Val P@5 | Val direct@5 | Val nDCG@5 |
|---:|---:|---:|---:|---:|---:|---:|
| 0.0 | 0.522 | 0.259 | 0.487 | 0.563 | 0.279 | 0.514 |
| 0.1 | 0.573 | 0.289 | 0.561 | 0.595 | 0.326 | 0.568 |
| 0.2 | 0.616 | 0.322 | 0.617 | 0.632 | 0.358 | 0.613 |
| 0.3 | 0.651 | 0.351 | 0.649 | 0.658 | 0.368 | 0.643 |
| 0.4 | 0.695 | 0.400 | 0.699 | 0.684 | 0.395 | 0.682 |
| **0.5** | 0.741 | 0.432 | 0.737 | 0.758 | 0.437 | 0.741 |
| **0.6** | **0.757** | **0.459** | **0.762** | 0.753 | 0.442 | 0.754 |
| 0.7 | 0.749 | 0.446 | 0.753 | 0.774 | 0.447 | 0.769 |
| 0.8 | 0.738 | 0.451 | 0.741 | 0.758 | 0.437 | 0.781 |
| 0.9 | 0.738 | 0.449 | 0.743 | 0.774 | 0.447 | 0.772 |
| 1.0 | 0.724 | 0.438 | 0.726 | 0.747 | 0.405 | 0.726 |

74 dev questions (37 targets), 38 validation questions (19 targets).

### Against the 0.5 baseline

Mean difference over targets, both forms of a question kept together; 95% bootstrap interval,
10,000 resamples. "w/l" counts questions that went up or down.

| Weight | Dev nDCG@5 | Val nDCG@5 | Val P@5 | Val direct@5 |
|---:|---|---|---|---|
| 0.4 | −0.037 [−0.070, −0.010] | −0.060 [−0.115, −0.015] | −0.074 [−0.147, −0.016] | −0.042 [−0.089, 0.000] |
| **0.6** | +0.025 [−0.001, +0.054], 22 w / 18 l | **+0.013 [+0.001, +0.026]**, 14 w / 7 l | −0.005 [−0.032, +0.021] | +0.005 [−0.011, +0.021] |
| 0.7 | +0.016 [−0.021, +0.054] | +0.028 [−0.003, +0.063] | +0.016 [−0.021, +0.058] | +0.011 [−0.026, +0.042] |
| 0.8 | +0.004 [−0.047, +0.054] | +0.039 [−0.002, +0.083] | 0.000 [−0.042, +0.047] | 0.000 [−0.042, +0.042] |
| 1.0 | −0.011 [−0.074, +0.050] | −0.015 [−0.105, +0.071] | −0.011 [−0.084, +0.063] | −0.032 [−0.084, +0.021] |

0.0 to 0.3 are further below 0.5 than 0.4 is, all intervals below zero on both halves; the full
table is in the JSON. 0.7 and 0.8 have higher validation means than 0.6 but were not the dev
choice and their intervals cross zero; choosing them from this table would be choosing on the
validation half.

### What changes between 0.5 and 0.6

The largest dev losses for 0.6 are questions that name the job. The posting with that exact title
moves from second to fourth: "AI Research Engineer at an AI-native multimodal lakehouse company"
(Q4) and "Majors Account Executive at a remote-first observability company" (Q6), where three
other Majors Account Executive postings graded 0 now come first. The largest wins describe the work:
"collecting and preparing information so machines can get better at comprehending
surroundings" (Q49) went
from two relevant postings in five to five, and "platforms to monitor and address online community
user protection issues" (Q71) from two partial matches to three. More Qwen weight trades a little on
named-title questions for more on paraphrased ones, as the known-target study found (0.6: 45
natural and 11 paraphrased targets in five, against 45 and 9 at 0.5).

## Limits

- **An offline blend, not the assistant.** The weights are weighted RRF (k = 60) of exact Qwen
  best-chunk cosine and BM25 over a fixed 2026-11-02 snapshot of 16,306 postings. The assistant's
  search (`retrieve()`) lets keywords decide relevance and has no single Qwen weight; this does
  not measure it, its filters or its speed.
- **Model grades.** One model, one rubric, one pass. The controls and repeats say the judge is
  consistent and not careless; they do not say it agrees with the owner.
- **Pooled to depth five.** nDCG is relative to the best five grades any weight found. A relevant
  posting no weight ranked in its top five is invisible.
- **Small, and not a pristine holdout.** 19 validation targets, and all 56 targets were inspected
  before this study, so validation is held out from weight choice only.
- **Two candidates a call for 336 pairs.** The repeats found 15 of 16 grades unchanged when graded
  alone.

## Files

- [`qwen-weights-27b-judged-2026-11-03.csv`](qwen-weights-27b-judged-2026-11-03.csv): every question
  at every weight, with its P@5, direct@5, nDCG@5, target and target rank.
- [`qwen-weights-27b-judged-2026-11-03.json`](qwen-weights-27b-judged-2026-11-03.json): the tables,
  every paired comparison and the reliability counts.
- [`qwen-weights-27b-grades-2026-11-03.csv`](qwen-weights-27b-grades-2026-11-03.csv): all 1,453
  grades (1,421 pool pairs, 16 negative controls, 16 repeats) with the judge's reason, posting id,
  batch size and time.
- Inputs: rankings SHA-256 `d40dcea5…5e67b`, pool `499e9f95…6b70`, export `8f7ea38d…6343`. The
  grading tools and audit trail are in `/private/tmp/jobrunner-qwen27b-judge`.
