# Phase 5: evaluate retrieval before changing ranking — report

Part of the plan in `docs/HARNESS_AUDIT_2026-10-15.md`, section H, Phase 5.
Based on `main`. Started on 2026-10-18.

Phase 5 has three parts.

| Part | State |
|---|---|
| 1. Fix proven filter-before-limit starvation (finding F, medium) | Merged in #180, measured on the live database |
| 2. Owner relevance on a frozen, held-out set | Merged in #181 and deployed 2026-10-19: migration applied, 330 postings pooled. Thresholds approved. Waits on the owner's grades, then `make score-judgments freeze=1`. Answer support is not built |
| 3. Evaluate the re-ranker and semantic switches, each on its own | Not started: needs part 2's frozen baseline |

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

## Part 3

The cross-encoder with candidates and the semantic switch, each measured
against part 2's frozen baseline, one switch at a time, the held-out half
read once at the end. Not shipped unless it beats the baseline under the
approved thresholds.
