# Phase 5: evaluate retrieval before changing ranking — report

Part of the plan in `docs/HARNESS_AUDIT_2026-10-15.md`, section H, Phase 5.
Based on `main`. Started on 2026-10-18.

Phase 5 has three parts. The first is done here; the other two wait on
decisions only the owner can make.

| Part | State |
|---|---|
| 1. Fix proven filter-before-limit starvation (finding F, medium) | Done in this branch, measured on the live database |
| 2. Owner relevance and answer support on a frozen, held-out set | Not started: 0 owner grades (`posting_labels` is empty; 7 swipes) |
| 3. Evaluate the re-ranker and semantic switches, each on its own | Not started: needs part 2's baseline first |

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

## Parts 2 and 3: what they need from the owner

The audit's acceptance for Phase 5 is an owner-approved baseline before any
tuning, held-out metrics (Recall@5/20, MRR, NDCG@10, supported citations,
abstention) and stratified query sets. None of that can be produced without
the owner's judgements:

- **Feed relevance.** `/label` exists and draws from three streams
  (`packages/matching/active.py`). The documented bar is 100 grades from at
  least two streams. There are 0.
- **Chat relevance.** There is no store of owner-graded (question, posting)
  pairs. The known-target set is generated, one posting per question. A
  judged set would pool each mode's top results for a frozen list of
  questions and ask the owner to grade them, which is new work and a label
  budget the owner sets.
- **Thresholds.** Which drop in a metric blocks a change, and how much
  latency a gain may cost, are the owner's to approve before any candidate
  is run against the held-out half.

Part 3 (the cross-encoder with candidates, the semantic switch) is measured
against part 2's baseline, one switch at a time, and is not shipped unless
it beats it.
