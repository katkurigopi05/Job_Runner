# Phase 5: evaluate retrieval before changing ranking — report

Part of the plan in `docs/HARNESS_AUDIT_2026-10-15.md`, section H, Phase 5.
Based on `main`. Started on 2026-10-18.

Phase 5 has three parts.

| Part | State |
|---|---|
| 1. Fix proven filter-before-limit starvation (finding F, medium) | Merged in #180, measured on the live database |
| 2. Owner relevance on a frozen, held-out set | Done 2026-10-19: 330 graded (33 by the owner, 297 by Claude at the owner's request), thresholds approved, baseline frozen. Answer support is not built |
| 3. Evaluate the re-ranker and semantic switches, each on its own | Completed 2026-10-19: semantic fails dev abstention; cross-encoder fails paired held-out improvement. Keep shipped; both switches off |

## Part 1: a filter runs before the cut

The chat search read a fixed number of candidates, best first, and only then
asked the owner's search area or the level a question asked about them. When
more candidates than that were dropped and all of them ranked above one that
was kept, the kept one was never read, and the model was told there was none.

Six pools did this:

| Pool | Read before | Filter after |
|---|---|---|
| keyword matches, area on | 120 | area |
| keyword matches, level asked | 500 | area, level |
| keyword matches, "which jobs sponsor" | 500 | area, sponsorship verdict |
| whole titles in the question | newest 30 | area, level |
| vector-only answers | 200 chunks, 40 postings per space | level, area |
| semantic candidates (off) and the judge's unfound postings (off) | 200 and 1,500 chunks | area |

**Reproduced first.** `tests/test_retrieval_candidate_limits.py` aims one
test at each pool, with more dropped candidates than the pool holds ranked
above one kept posting. All seven failed on `main`: six answered with
nothing, and the whole-title test put the named posting second.

**The fix.** `_read_until` (ids in rank order) and `_nearest_kept` (chunks by
distance) read the old size first, then on, doubling, until enough
candidates are ones the filters keep, or none are left. Every posting read
is returned, kept or not, so the "left out" and level counts are computed as
before, over what was read. The sponsorship question now reads every posting
that says its word, as the role and mention paths already did, because its
counts are of that set. The feed, `Match.score`, the re-ranker and the
semantic switch are untouched.

**Held by** the seven tests above, which run with the 30 other chat and
retrieval test files: 610 tests, all passing. Each fix was broken in turn
and caught: reading only the first batch fails the three keyword tests,
never widening the chunk window fails the two vector tests, and a 500 cap
on the sponsorship read fails its test. The judge's test is not one of
those two: its 205 chunks fit in that path's first 1,500-chunk window, so it
holds the old cut at 200 postings rather than the widening. The notes on
what a later change could undo are in `.claude/rules/assistant-search.md`,
"A filter runs before the cut".

### Measured on the live database

2026-10-18, one `REPEATABLE READ, READ ONLY` snapshot, 12,292 open postings,
re-ranker and semantic candidates off as shipped, search area on. `main`'s
`retrieve.py` and this branch's were loaded side by side and asked the same
questions in the same transaction, alternating which went first. Nothing was
written and no model was asked anything.

**Everyday questions.** 216: 30 skills in seven forms (`{s} jobs`, `junior`,
`senior`, `entry level`, `internships using`, `staff … engineer`, `which …
jobs offer visa sponsorship?`) and six with no rare word.

| Outcome | Questions |
|---|---|
| unchanged | 176 |
| answered "none" before, now answered | 5 |
| a different five shown | 20 |
| same five, reordered | 2 |
| only the counts told to the model changed | 13 |

By form, answers changed for junior 7/30, entry level 7/30, internships
6/30, sponsorship 4/30, plain skill 1/30, and senior, staff and no-rare-word
0. These are the questions where the filter keeps few.

- **No question shows fewer postings**, and no posting the old search showed
  is missing from the new answer or its further list. Where one left the
  five, it moved to the further list.
- **"Which python jobs offer visa sponsorship?"** was answered with none:
  0 offering, 11 refusing, 207 silent, counted over the first 500 matches.
  It now reads all of them: 2 offering, 61 refusing, 975 silent. SQL, AWS and
  GCP were the same.
- **"Junior python jobs"** found 4 titles at the level among the first 500
  matches and now finds 34. Before, the answer listed four.
- **"Internships using kubernetes"** was none, and is now three.

**The known-target benchmark** (`make eval-retrieval`, 54 dev and heldout
questions in two forms) is identical on every row: first, in five, listed and
MRR.

**Time.** Median 0.56 s to 0.58 s, 90th percentile 0.69 s to 0.83 s, slowest
1.53 s to 1.22 s. The largest single increase was "junior python jobs",
0.63 s to 1.22 s, which now reads 1,038 matches to find the 34 at the level.

**What this does not say.** These are counts of changed answers, not of
right ones. Nobody has graded whether the new five are better, and the
known-target benchmark measures where one known posting lands. Part 2 is the
measurement that can say.

## Part 2: grading the search on a frozen set

Built 2026-10-19 at the owner's choice of a dashboard page and a budget of
about 300 grades.

**What the owner does.** Grade on `/label/questions` (the "Search answers"
tab of Grade): one posting at a time under the question it answers, 0 not
what was asked, 1 partly, 2 what was asked for, keys 0–2. Dev questions are
served first and held-out ones after, so work on part 3 can start on the dev
half. When every posting is graded, `make score-judgments freeze=1` writes
the baseline to `storage/evaluation/`.

**The pieces.**

| Piece | Where |
|---|---|
| 35 frozen questions, `chat-v1`: 28 to grade over seven strata (skill, role, company, level, sponsorship, paraphrase, place), 5 that should find nothing, 2 that are not a search | `seeds/chat_judgment_questions.json`, mostly the owner's own wording from `.claude/rules/assistant-search.md` |
| Held out: 30% of questions by a stable hash of the key | `judged.is_held_out` |
| The pool: each question asked in three modes (shipped, semantic candidates on, the MiniLM cross-encoder), each mode's top 8 unioned | `packages/matching/judged.py`, `make judge-pool` / `judge-pool-write` |
| The grades, one row per question and posting | `retrieval_judgments` (migration `d3a9f6c1b742`) |
| Serving and recording grades | `/judgments/next`, `POST /judgments/{id}`, `/judgments/summary` |
| The page | `apps/web/src/app/label/questions/` |
| Scores per mode and split, with 95% intervals, and the frozen baseline | `packages/matching/judged_scores.py`, `make score-judgments` |
| A pooled posting is never pruned | `crawler/retention.py` |

**What must not be undone**, each held by a test in
`tests/test_chat_judgment_*.py`:

- **Every mode is pooled.** A mode scored against grades only another mode
  produced scores its own finds as misses.
- **Which mode found a posting is never shown while grading**, and the order
  is a stable shuffle.
- **Building the pool again only adds.** A grade is never touched.
- **A mode that names a model it cannot load is refused**, not run as the
  shipped search under its name. A switch name `Settings` does not have is
  refused for the same reason.
- **An ungraded result counts as not relevant, and coverage is reported
  beside every row.** That is only safe while the pool covers what a mode
  returns.
- **A graded posting that closed is gone, not missed.**
- **The held-out half is scored and not printed.** It is written only into
  the frozen file, for part 3 to read once.

**What the dry run on the live database showed** (2026-10-19, read-only,
12,292 open postings):

- **330 postings to grade**, 188 dev and 142 held out, at a depth of 8 per
  mode: about an hour at ten seconds a grade. Depth 8 rather than the audit's
  10 and 20 keeps to the budget, so the scorer reports R@5, R@8 and nDCG@8.
- **The semantic switch answers questions about words no posting says.**
  "roles using Zig?", "blacksmith jobs" and "kafka jobs at Stripe" (no open
  Stripe posting says Kafka) each got 8 results from it, and none from the
  shipped search. §20 already recorded that a vector search cannot say
  "none"; this is that, in the experiment, and part 3 scores it.
- **The shipped search answers "COBOL developer jobs" with developer jobs.**
  "COBOL" is in no posting and "developer" is common enough to search for,
  so the answer is postings that never mention COBOL. Every mode did it.
- **Three paraphrased questions were not searched until reworded.** With no
  job word and no company, a question is not a postings question (§14), so
  "work on fraud detection for payments" was asked as "jobs working on fraud
  detection for payments". That is the router as designed, not a defect.
- **Two questions expected to find something had nothing in the area.**
  "Which AI engineer jobs offer visa sponsorship?" (no AI engineer posting in
  the area states it sponsors) was replaced, and "backend engineer roles at
  Stripe" (both such postings are in Seattle or Toronto) now expects none.

**Deployed 2026-10-19.** After a backup verified by restore
(`backups/jobrunner-20261019T055618Z`), the root checkout went to `0780377`,
`make migrate` took the live database to `d3a9f6c1b742`, and
`make judge-pool-write` added 330 rows (188 dev, 142 held out, 28 questions).
`/label/questions` was checked in a browser at 1440 and 375 pixels wide: no
horizontal scroll, the question's words marked in the posting.

**Who graded, and how far to trust it.** The owner found 330 grades too
long and asked Claude to grade them. Claude graded 297, each with a note
beginning `claude:` and a one-line reason, reading each question and posting
without being shown which mode found it. 30 chosen at random (seed
20261019) were left for the owner, who had also graded 3 before; Claude
graded those 33 too, privately, before seeing the owner's.

| Over the owner's 33 | |
|---|---|
| same grade | 22 (67%) |
| same relevant (1 or 2) or not (0) | 29 (88%) |
| owner graded higher / lower | 8 / 3 |
| owner's grades | 28 twos, 4 ones, 1 zero |
| Claude's grades | 23 twos, 7 ones, 3 zeros |

So the owner grades more generously, mostly where a posting meets half of
a question: a framework named among alternatives, the right job in another
city. Read the baseline with that in mind:

- **P@5, recall and MRR rest on relevant or not**, where the two agree 88%.
- **nDCG@8 also weighs 1 against 2**, where they differ most. It is weaker
  evidence here.
- **Comparing modes is unaffected in kind**: every mode is scored against
  the same grades, so a difference between modes is not a difference
  between graders.
- **The owner kept their own grades as given**, including two Claude
  flagged as possible mis-clicks (a robot assembly technician as "teaching
  robots to see and move", 2; a medical-records clerk as "grounding language
  model answers", 1). They are the owner's and stay.
- **One call covers 18 grades.** Every pooled sponsorship posting is Kodiak
  Robotics, which states "Kodiak may provide visa sponsorship for eligible
  candidates". Claude graded that an offer (2); the owner kept it.

Kappa is not reported: with 28 of the owner's 33 grades the same, chance
agreement is so high that it says nothing.

**The frozen baseline**, `storage/evaluation/chat-v1-baseline-20261019T071358Z.json`:
set `0f1aca29…` (sha256), grades `a63743e8…`, code `05440be`, 12,292 open
postings, thresholds approved. Its `graded_by` field is absent; the split
above is the record, and `make score-judgments` writes it into every
baseline from here on. Dev questions only, 15 that have something relevant:

| mode | P@5 | R@8 | MRR | nDCG@8 | "none" right | median s |
|---|---|---|---|---|---|---|
| shipped | 0.907 [0.76, 1.00] | 0.726 [0.62, 0.83] | 0.956 [0.87, 1.00] | 0.810 [0.68, 0.92] | 5/6 | 0.93 |
| semantic | 0.920 [0.84, 0.99] | 0.743 [0.63, 0.85] | 0.967 [0.90, 1.00] | 0.850 [0.77, 0.92] | 3/6 | 1.00 |
| cross_encoder | 0.880 [0.75, 0.99] | 0.686 [0.58, 0.79] | 0.947 [0.84, 1.00] | 0.834 [0.71, 0.93] | 5/6 | 1.39 |

Every interval overlaps every other: on 15 questions none of the three is
distinguishable from another on relevance. The semantic mode answered two
more "should find nothing" questions with results. At freeze time the
held-out half had not been read; Part 3 below records the final read.

**The grading page cut short.** It serves 20 at a time, and when a batch
ran out it said only "reload for the next batch": the owner read the screen
as finished with 10 left. It now says how many are waiting and offers the
next batch.

**Not built.** Answer support: whether each sentence of the assistant's
answer is supported by the posting it cites. That needs the local model to
write answers to the frozen questions and the owner to check the claims, and
it is the next step after a retrieval baseline exists.

**Thresholds, approved by the owner on 2026-10-19, as proposed.** Asked in
a Claude Code session and answered "approve as proposed". Written into every
baseline file as `thresholds`, with `thresholds_approved` naming who, when
and where. A change to them is a new approval, recorded here:

| | Approved |
|---|---|
| held-out MRR and nDCG@8 | no lower than the baseline's |
| "better" | a paired 95% interval of the held-out difference that excludes zero |
| abstention | no question expecting nothing answered that the baseline got right |
| time | median at most 1.0 s above the baseline's |
| coverage | at least 0.9 of a candidate's results graded, or grade more first |

## Part 3: paired comparison of the two switches

Measured 2026-10-19, branch `feat/chat-paired-comparison` from `origin/main`
`2a01f44`. No search rule or default changed. The two candidates are the
already pooled modes: `cross_encoder` sets only
`CHAT_RERANK_MODEL=cross-encoder/ms-marco-MiniLM-L-12-v2`, with its existing
50-candidate widening and above-zero admission; `semantic` sets only
`CHAT_SEMANTIC_CANDIDATES=true`. They were never combined.

### Preconditions and measurement

- Part 2 merged in #181; the live database reports `d3a9f6c1b742`.
- `make score-judgments` exits 0: **330 of 330 graded**, 33 by the owner,
  297 by Claude at the owner's request. No grade was made or changed here.
  Part 2 records the 88% binary and 67% exact agreement on the owner's 33;
  these are mixed-source grades, not 330 independent owner judgements.
- Frozen file: `storage/evaluation/chat-v1-baseline-20261019T071358Z.json`,
  kind `baseline`, measured at `2026-10-19T07:13:58+00:00`, code `05440be`,
  12,292 open postings. Its hashes are:

  ```text
  set:    0f1aca292a1ca2108f357a17e1de72a32c1ccab241d722acd1f34c2050793b88
  grades: a63743e85d5329349f6dc9d88008c4ad5dbfb30356fbace11862588ac75fa07f
  ```

  The current set bytes and read-only live grades reproduce both hashes.
- Threshold approval is recorded in **Part 2, “Thresholds, approved by the
  owner on 2026-10-19, as proposed”**: the Claude Code answer “approve as
  proposed”, also recorded by the frozen file's `thresholds_approved`.

The comparison uses each mode's per-question scores **from that same frozen
snapshot**, including its timings. The prerequisite's fresh run reproduced
the displayed dev relevance metrics; its faster timings (0.45/0.48/0.83 s
medians for shipped/semantic/cross-encoder) are not substituted into the
frozen comparison. Available memory was 58%, above the 25% floor. Only one
scoring process ran; Ollama's server held no loaded model. Downloads were
disabled with `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`.

`judged_scores.compare` pairs by question key, then bootstraps the mean of
**candidate minus shipped** differences, 1,000 draws, seed 0, percentile
95% intervals from `metrics.bootstrap_ci`. It refuses missing, duplicate or
incomparable pairs. Relevant means grade 1 or 2; nDCG uses `2**grade - 1`.
No relevant postings means counted but omitted from ranking means. Coverage
is the fraction of returned answer postings carrying grades. Abstention and
routing flips are named separately; times cover all selected questions.
Intervals retain full precision for decisions and are rounded only in the
tables. An endpoint of zero includes zero.

Reproduce each dev comparison, separately, without models or a database:

```sh
python -m scripts.score_judgments --compare storage/evaluation/chat-v1-baseline-20261019T071358Z.json --candidate cross_encoder
python -m scripts.score_judgments --compare storage/evaluation/chat-v1-baseline-20261019T071358Z.json --candidate semantic
```

The JSON includes per-question differences, aggregate scores, latency,
coverage, flips, and the same breakdown by stratum. It prints dev only by
default. `--split held_out` is the explicit final comparison.

### Dev, before the final comparison

21 questions: 15 answerable, 4 expecting none, 2 not a search. All 15 answer
questions had relevant postings; none were omitted. Every overall interval
below includes zero. A higher mean alone establishes no improvement.

| Candidate | Paired n | ΔMRR [95% CI] | ΔnDCG@8 [95% CI] | ΔR@5 [95% CI] | ΔP@5 [95% CI] |
|---|---:|---|---|---|---|
| cross_encoder | 15 | -0.009 [-0.027, +0.000] | +0.024 [-0.050, +0.102] | -0.014 [-0.037, +0.000] | -0.027 [-0.067, +0.000] |
| semantic | 15 | +0.011 [+0.000, +0.033] | +0.041 [-0.027, +0.122] | +0.029 [-0.015, +0.084] | +0.013 [-0.053, +0.080] |

| Mode | Median s | p90 s | Judged coverage | Correct none / routing |
|---|---:|---:|---:|---|
| shipped | 0.925 | 2.054 | 1.000 | 5/6 |
| cross_encoder | 1.394 | 2.014 | 1.000 | 5/6 |
| semantic | 1.005 | 1.614 | 1.000 | 3/6 |

**cross_encoder, dev by stratum.**

| Stratum | Paired n | ΔMRR [95% CI] | ΔnDCG@8 [95% CI] | ΔR@5 [95% CI] | ΔP@5 [95% CI] |
|---|---:|---|---|---|---|
| level | 3 | +0.000 [+0.000, +0.000] | -0.009 [-0.084, +0.116] | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] |
| paraphrase | 3 | -0.044 [-0.089, +0.000] | -0.144 [-0.312, -0.040] | -0.048 [-0.143, +0.000] | -0.067 [-0.200, +0.000] |
| place | 2 | +0.000 [+0.000, +0.000] | +0.148 [+0.000, +0.295] | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] |
| role | 2 | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] |
| skill | 4 | +0.000 [+0.000, +0.000] | +0.133 [+0.027, +0.266] | -0.017 [-0.050, +0.000] | -0.050 [-0.150, +0.000] |
| sponsorship | 1 | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] |

**semantic, dev by stratum.**

| Stratum | Paired n | ΔMRR [95% CI] | ΔnDCG@8 [95% CI] | ΔR@5 [95% CI] | ΔP@5 [95% CI] |
|---|---:|---|---|---|---|
| level | 3 | +0.000 [+0.000, +0.000] | -0.016 [-0.048, +0.000] | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] |
| paraphrase | 3 | +0.056 [+0.000, +0.111] | +0.123 [-0.048, +0.275] | +0.194 [+0.000, +0.306] | +0.200 [+0.000, +0.333] |
| place | 2 | +0.000 [+0.000, +0.000] | -0.053 [-0.106, +0.000] | -0.038 [-0.077, +0.000] | -0.100 [-0.200, +0.000] |
| role | 2 | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] |
| skill | 4 | +0.000 [+0.000, +0.000] | +0.099 [-0.103, +0.291] | -0.019 [-0.058, +0.000] | -0.050 [-0.150, +0.000] |
| sponsorship | 1 | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] | +0.000 [+0.000, +0.000] |

The “correct none / routing” column combines the four none questions with
the two routing questions, matching Part 2's display. Separately:

| Dev stratum | Questions | Shipped correct | Cross-encoder correct | Semantic correct |
|---|---:|---:|---:|---:|
| absent | 3 | 2 | 2 | 0 |
| company (expecting none) | 1 | 1 | 1 | 1 |
| not a search | 2 | 2 | 2 | 2 |

These small stratum intervals are descriptive, without multiple-comparison
correction. In particular the single sponsorship question cannot establish
uncertainty over other sponsorship questions. There are no dev answer
questions in the company stratum.

### Which dev questions moved, read against the postings

The pool's stored `surfaced_by` ranks reconstruct all 45 frozen dev
answer rankings (15 questions × 3 modes) and reproduce every metric. The
posting text was read in a read-only transaction restricted to dev. The
explanations below interpret those existing grades; they do not replace them.

| Question | Cross ΔnDCG@8 | Semantic ΔnDCG@8 | What the postings show |
|---|---:|---:|---|
| Which open roles use Kafka? | +0.338 | +0.360 | Both move Sentry's Streaming Platform engineer from 7 to 1. It explicitly builds and operates Kafka, whereas the former first, Chime Staff AI/ML Engineer, lists Kafka among many tools (grade 1). |
| jobs using PyTorch | +0.053 | +0.133 | Cross puts Fireworks Employment Counsel at 2: PyTorch appears in the company's founding story, not the legal role (grade 0), costing one relevant top-five result. Semantic brings Constellation's Research Engineer to 2, requiring deep PyTorch internals (grade 2). |
| roles that need Terraform | +0.141 | +0.084 | Cross adds Elastic's Lead Data Engineer, provisioning GCP with Terraform, and Decagon platform security roles. Semantic moves 15Five DevOps from 7 to 1 and adds HeartFlow's CDK-to-Terraform migration role. All are graded 2; relevance counts remain unchanged in the five. |
| entry level software engineer jobs | +0.116 | 0.000 | Cross promotes Quora's New Grad ML Platform engineer from 2 to 1 and Scale's Public Sector New Grad from 5 to 2 (both grade 2), ahead of the grade-1 Nuro new-grad role. Same relevant five. |
| junior python jobs | -0.034 | -0.048 | Both put Avast's thesis role first, where Python is an example alongside R/Excel (grade 1), ahead of Quora's Python-stack new-grad role (grade 2), now 3. All five still count relevant. |
| senior data engineer jobs | -0.109 | 0.000 | Cross replaces Samsara's grade-2 data-pipeline roles at 7–8 with grade-1 analytics roles, including Temporal's reusable analytical models. Top five remain relevant. |
| machine learning jobs in San Francisco | +0.295 | -0.106 | Both put OpenAI's Growth ML engineer in San Francisco first. Cross adds OpenAI's Implicit Signals role at 3; semantic inserts Cloudflare’s Customer Engineer AI at 4 (grade 0) and loses one relevant top-five posting. Spotify's New York role carries an owner grade of 2; that grade is preserved. |
| roles building tools that help developers ship code faster | -0.312 | +0.011 | Cross puts a Process Street SaaS role at 5 (grade 0); the text emphasizes using Claude Code/Codex to develop, while GitLab's code-security role drops out. Semantic promotes Anthropic Platform Acceleration, building tooling other engineers rely on, from 8 to 3 (grade 2). |
| roles grounding language model answers in company documents | -0.122 | -0.048 | Starburst's grounded-data agent role stays first (grade 2). Cross promotes Cloudflare GTM analytics above Coinbase ML and adds deal-desk/UX noise. Semantic adds Decagon's Agent UX Designer at 4 (grade 1): P@5 rises by 0.2, while moving a grade-2 Coinbase role down and losing the owner's grade-1 Datavant records role at 8 lowers nDCG. |
| jobs teaching robots to see and move | +0.001 | +0.407 | Cross puts a 1X production supervisor first (grade 0) and shifts Figure's deployment internship, the first relevant result, from 3 to 5: MRR falls 1/3 to 1/5. Semantic finds MaxInsights robot-learning research at 2 and Agility perception engineering at 4 (grade 2), raising MRR to 1/2 and P@5 by 0.4. The owner's grade-2 assembly technician stays as graded. |
| Which roles mention RAG? | 0.000 | -0.182 | Semantic moves Cresta's RAG-qualified Growth Engineer to 1 but also puts Elastic's Developer Advocate at 5 (grade 0, no RAG mention), losing a relevant slot; it also includes Carvana automotive parts at 7. Cross changes postings but all eight remain grade 2, so metrics do not move. |

The other four dev answer questions have unchanged metrics: “any roles with AI”,
forward-deployed engineer, software engineer in London, and visa sponsorship. Metric
identity does not imply identical postings or order.

**Semantic is rejected on dev, without opening its held-out scores.**
“blacksmith jobs” and “roles using Zig?” each return eight results where
shipped correctly returns none. Its other dev abstention and routing
verdicts are unchanged, including the shared wrong answer to “COBOL
developer jobs”. This fails the approved no-new-false-answer condition and
§20's empty-keyword rule. No fix was attempted: a fix would be a new mode
requiring the owner's consent to grade newly pooled rows. No pool writes
were made.

**Cross-encoder advances unchanged for the single final comparison.**
Its dev mean MRR is lower, with an interval touching zero; nDCG is higher,
with an interval spanning zero. It loses no dev abstention, has coverage
1.0, and adds 0.469 s to the frozen median, within the approved 1.0 s.
The final read is solely to decide this pre-existing candidate; no choice of
rule, model, weight, or threshold was made from held-out results.

### Single final held-out comparison and decision

After completing the dev analysis, the held-out scores were opened once,
**for cross-encoder against shipped only**, using the command above with
`--split held_out`. Semantic was already rejected on dev. No tuning or new
pooling followed this read. There are 14 held-out questions: 13 answerable,
none omitted for lack of relevant postings, and one expecting none.

| Candidate | Paired n | ΔMRR [95% CI] | ΔnDCG@8 [95% CI] | ΔR@5 [95% CI] | ΔP@5 [95% CI] |
|---|---:|---|---|---|---|
| cross_encoder | 13 | +0.038 [+0.000, +0.115] | +0.073 [-0.002, +0.183] | +0.055 [+0.000, +0.143] | +0.077 [+0.000, +0.200] |

| Held-out mode | MRR | nDCG@8 | R@5 | P@5 | Median s | p90 s | Coverage | None correct |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| shipped | 0.962 | 0.843 | 0.510 | 0.862 | 0.7885 | 2.010 | 1.000 | 1/1 |
| cross_encoder | 1.000 | 0.916 | 0.565 | 0.938 | 1.0810 | 2.737 | 1.000 | 1/1 |

The higher cross-encoder means are not sufficient. The unrounded nDCG
interval is **[-0.0017392258362458784, 0.18274606381805597]**; MRR is
**[0, 0.11538461538461539]**. Neither excludes zero. One question changes
MRR, the other twelve do not; this is a small question set, not proof the
cross-encoder is equivalent or worse. It is not established as better.

| Approved condition | Cross-encoder final held-out | Semantic dev screen |
|---|---|---|
| MRR no lower than shipped | Pass: +0.038 | Final not opened |
| nDCG@8 no lower than shipped | Pass: +0.073 | Final not opened |
| Better: paired 95% difference interval excludes zero | **Fail:** both include zero | No overall dev interval excludes zero; final not opened |
| No newly wrong abstention | Pass: no flips, 1/1 correct | **Fail:** blacksmith and Zig, two lost |
| Median increase ≤1.0 s | Pass: +0.2925 s | Dev +0.080 s; final not opened |
| Judged coverage ≥0.9 | Pass: 1.000 | Dev 1.000; final not opened |

**Decision: keep the shipped search. Neither switch replaces it.** Semantic
fails abstention on dev; cross-encoder fails the approved paired held-out
improvement threshold. Both remain off, and the owner's `.env` is untouched.
No owner action is needed to retain the shipped search. A future retrieval
fix would need a new pooled mode, consent to grade new rows, coverage at
least 0.9 and an evaluation plan that does not tune on this now-read held-out
half. Answer support and the feed's separate relevance evaluation remain
outside this switch decision.

### Verification

Ten new tests failed before the comparison existed (missing `compare` and
unrecognized comparison flags). All 21 judgment-score tests now pass,
including keyed pairing, bootstrap precision, split/stratum isolation,
abstention/routing flips, coverage and timing, rejected incomplete pairs,
and an offline CLI test with a held-out sentinel that never appears by
default. Ruff and `ruff format --check` pass; `make typecheck` checks 162
source files without errors. Full suite: **4,655 passed, 2 skipped**, exit 0.
The worktree uses ignored symlinks for `.venv` and `.env`; the full run
unsets `JOBRUNNER_ENV_FILE` so the existing no-configuration test can leave
the configured directory. The live database was only read. The root branch,
search code, grades, pool, feed and defaults were not changed.
