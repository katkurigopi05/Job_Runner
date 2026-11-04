# Kaggle model evaluation: October 28, 2026

Direct Qwen3.8 27B UD-Q6_K remains the best-supported answer baseline from these
experiments. It corrected two clearly labeled no-match failures observed with
8B. Refinement added no correct answers on the later twelve-question diagnostic.
Swift and Gemma were tested as source reviewers; neither has a matched direct
answer result demonstrating that it should replace the original 27B model.

This is a summary of completed, sealed local experiments. Publishing this report
made no model calls. Raw prompts, source excerpts, source mappings, rendered
answers, credentials and owner records remain in private local storage.

## Answer generation

| Phase | Scope | Observed result | Calls |
|---|---|---|---:|
| 8B post-fix baseline | 54 questions, including 21 selection questions; only three clear semantic reference questions | Correct on 1/3 distinct labeled questions, or 3/9 repeated observations; 0/6 correct empty abstentions | 64 |
| Original 27B comparison | Same questions and ordered catalogues as 8B | Correct on 3/3 distinct labeled questions, or 9/9 repeated observations; 6/6 correct empty abstentions | 64 evaluation + 1 startup |
| Direct 27B diagnostic | Twelve targeted questions sharing six catalogues and 21 postings | 12/12 reference agreement, including 4/4 correct empty abstentions | 12, included in the next row |
| Per-question refinement | Same twelve diagnostic questions; direct plus proposal, probe and composition | 12/12 agreement; no source sets changed and no gains or regressions | 48 total, including the twelve direct calls |
| Clean-context 27B rerun | Same twelve questions, with explicit KV/prompt clearing | All twelve source sets and raw outputs unchanged | 12 |

Both 8B and 27B produced 63/63 format-valid selection outputs and were stable on
21/21 repeated questions in the matched comparison. Format validity therefore
did not distinguish the incorrect 8B selections from the corrected 27B answers.
Ten of the 21 selected sets changed, but eight changed questions lacked clear
references and cannot be counted as quality gains.

Direct selection had a 3.65-second median on the diagnostic. The four-call
refinement pipeline had a 15.88-second median: 4.35 times the time and four times
the calls, with no observed answer improvement. All twelve policy proposals
chose `direct_evidence`; useful policy specialization was not demonstrated.

## Blind source-reference review

All three reviewers received identical system/user hashes on the same twelve
cases, with shuffled questions and source ordering and opaque source IDs. Prior
answers, labels and ID mappings were withheld. Each capture was sealed before a
separate offline comparison opened the earlier references.

Review runs used a 4096-token context, thinking disabled, temperature zero, seed
42 and a 1024-token answer limit. Fourteen context clears were verified per run,
with zero active context tokens at completion. There were no review retries or
output-repair calls.

| Reviewer | Strictly valid | Reference agreements | Invalid | Median call time | Sum of twelve timed calls |
|---|---:|---:|---:|---:|---:|
| Original Qwen3.8 27B UD-Q6_K | 12/12 | 12/12 | 0 | 25.09 s | 300.98 s |
| Swift-Qwen3.8 27B Q6_K | 12/12 | 12/12 | 0 | 24.05 s | 299.54 s |
| Gemma 4 31B IT Q5_K_M | 10/12 | 10/12 | 2 | 16.10 s | 209.34 s |

Gemma's two invalid responses enclosed JSON in Markdown fences, violating the
frozen JSON-only contract. Deterministic inspection after capture found that
both inner objects passed the unchanged checks, including literal quotations,
and agreed with the references. This content-only diagnostic does not change
the official Gemma result: **10/12 strict passes**, with no disagreements among
the ten scorable reviews. No ambiguity flags were present, including in the two
diagnostic objects.

The original reviewer is the evaluated model. Swift shares its Qwen3.8 base.
Gemma adds a different model family, but overlapping training data and common
interpretation errors remain possible. Human adjudication is still pending.

These sequential, single-run timings include context clearing and provider
overhead and use different checkpoints, quantizations and amounts of generated
explanation. They are not GPU-only throughput measurements or a general speed
ranking. Thinking was disabled, so Swift's reasoning-token efficiency was not
evaluated.

## Artifact identities and resources

| Model artifact | Revision | SHA-256 | Weight bytes |
|---|---|---|---:|
| `unsloth/Qwen3.8-27B-GGUF:UD-Q6_K` | `4ca720788d1e01f1bff70c033e0d0028fd02e502` | `c9c206812fbe4ac7b76a729e25928b63f2ae89d37f69da7a71c20aec763cd436` | 21,983,677,344 |
| `ukisai/Swift-Qwen3.8-27B-GGUF:Q6_K` | `98a6100719e3aafa186041089b71c7b48390dbe1` | `7f4de8abd5446c08b0f975a1b38e43d02639f4ae9ecdd2ddfd5c5b0f612bda59` | 22,884,407,456 |
| `unsloth/gemma-4-31B-it-GGUF:Q5_K_M` | `c1ac76e99d5513b141e8adde7288b85c3f9c32ec` | `95192e41190ef5eb72db92b669cea0ed733049c2265a03a64ac5a080d8605958` | 21,658,401,728 |

The GGUF models used llama-cpp-python 0.3.36 and two T4 GPUs with a 45%/55%
layer split. Both Qwen variants and Gemma fit the observed setup at a 4096-token
context. These observations do not measure peak inference memory, isolate model
size from quantization/backend effects, or establish the best quantization.

## Call accounting and evidence limits

The October 28 UTC audit reconciled to 214 calls through the Swift review:
64 for the 8B phase, 65 for the original 27B comparison, 48 for direct plus
refinement, one integration synthetic call, twelve clean-context calls, twelve
Qwen review calls and twelve Swift review calls. The Gemma phase added twelve
blind calls and one synthetic readiness call, bringing that snapshot to 227.
A later Qwen reload added one synthetic call, bringing usage to 228. Reload
readiness calls are operational checks and are not additional evaluation cases.

The twelve direct diagnostic calls are already included in the 48-call
refinement phase. Repeated observations, clean-context reruns and reviews reuse
questions and cannot be pooled into an overall accuracy percentage. The old
semantic comparison has only three clear provisional reference questions; the
later twelve-case set was deliberately constructed and is not representative
of production traffic. The same assistant authored its questions and initial
labels. Human calibration and a fresh frozen cohort remain pending.

These tests do not establish full retrieval recall, full-posting entailment,
whole-database count accuracy, mail correctness, long-context performance or
unrestricted prose quality. Engineering test passes are separate from model
quality evidence. No automatic model-quality promotion follows from this report.

## Local provenance

The private evidence families under `storage/exports/` are:

- `qwen38_q6_comparison_2026-10-28`: matched comparison and deployment identity.
- `qwen38_per_question_2026-10-28`: direct/refinement capture and references.
- `qwen38_context_reset_rerun_2026-10-28`: clean-context replay.
- `qwen38_blind_reference_review_2026-10-28`: original-model blind review.
- `swift_q6_review_2026-10-28`: Swift blind review.
- `gemma_q5_blind_review_2026-10-28`: Gemma review and format diagnostics.
- `harness_results_comparison_2026-10-28`: consolidated saved-result comparison.
- `qwen27_restore_2026-10-28`: subsequent model switch and readiness check.

The consolidated report recorded 74 successful stored hash assertions with no
mismatches; the Gemma completion checks recorded 63 assertions over their listed
manifests, plus archive, synthetic, frozen-plan and identical-input checks. These
overlapping checks are provenance evidence, not additional test cases. Original
captures, labels and seals were preserved. The raw evidence is intentionally not
committed, so readers of this public summary cannot independently reproduce its
scores without access to the owner's local artifacts.
