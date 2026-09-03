# Backlog — what the master spec asks for and this repo does not have

A gap register against the 67-section job-discovery specification, and the
buildable projects that close it.

Companion to [PARITY.md](PARITY.md), which maps this repo against career-ops.
This one maps it against the spec instead, so the two overlap only where both
sources want the same thing.

**Every row here was checked against the code**, not inferred from the spec.
Where something partly exists the row names the file and the gap, because
"missing" and "there but not wired" need different work.

---

## Status key

Same as PARITY.md, plus one:

| | |
|---|---|
| **HAVE** | exists and is tested |
| **PARTIAL** | some of it exists; the gap is named |
| **BUILD** | missing, wanted, nothing blocks it |
| **BLOCKED** | missing, wanted, and something must land first |
| **REFUSED** | a rule in CLAUDE.md §2 or §11 forbids it |

---

## Register

### Discovery and normalization (spec §5–§9, §39–§40)

| Spec | Capability | Status | Notes |
|---|---|---|---|
| §5 | Greenhouse, Lever, Ashby, Workable adapters | **HAVE** | `packages/ats/` |
| §5 | SmartRecruiters, iCIMS, SuccessFactors, Oracle, Jobvite | **BUILD** | → **P12** |
| §5 | Workday adapter | **REFUSED** | CLAUDE.md §11 puts it out of scope until after Phase 6 |
| §5 | Generic fallback crawler | **PARTIAL** | `crawler/jsonld.py` reads schema.org `JobPosting` off a bespoke page; `make probe-bespoke` promotes the pages that publish it. No sitemap path yet, and a page publishing nothing stays unreadable |
| §5 | robots.txt, rate limits | **HAVE** | `crawler/robots.py`, `ratelimit.py` |
| §5 | CAPTCHA bypass | **REFUSED** | CLAUDE.md §2.5, hard scope boundary |
| §6 | Common job schema | **PARTIAL** | `Posting` holds 13 columns against the spec's ~40 fields. No salary, no split required/preferred skills, no parsed education or experience → **P7** |
| §7 | Relevance beyond the title | **HAVE** | `matching/score.py` weights body 0.65, `roles.py` aliases titles |
| §8 | Multi-category job taxonomy | **PARTIAL** | `roles.py` maps a title to exactly one canonical role; the spec wants a job in several → **P11** |
| §9 | Internship / early-career detection | **PARTIAL** | `filters.py::detect_seniority` reads intern/junior/new-grad markers from the title. Not a first-class entity, and minimum-experience text is never parsed → **P7** |
| §39 | Canonical job entity, cross-board dedupe | **BUILD** | `content_hash` is per-posting change detection and `legitimacy.py` is ghost-job scoring — neither merges the same job seen on two boards → **P6** |
| §40 | Lifecycle: reposted, salary changed, requirements changed | **PARTIAL** | `first_seen_at`/`closed_at` exist; no `job_versions`, so a changed posting overwrites its own history → **P6** |

### Matching and ML (spec §1–§4, §12, §16, §35, §38, §44, §46–§48, §57)

| Spec | Capability | Status | Notes |
|---|---|---|---|
| §2, §47 | Ranking metrics, experiment records | **HAVE** | `matching/metrics.py`, `benchmark.py` — see [ML_EVALUATION.md](ML_EVALUATION.md) |
| §4 | Leak-safe splits, versioned datasets | **HAVE** | `matching/labels.py` |
| §45 | Blind evaluation | **PARTIAL** | Enforced for ranking variants; the résumé evaluators are blind by construction but nothing asserts it → **P4** |
| §2, §58 | Real labeled data | **BUILD** | 32 fixture labels, one profile. **The blocker on every ML claim here** → **P1** |
| §3, §46 | Model bake-off: LR, XGBoost, LightGBM, LTR, cross-encoder | **BLOCKED** | The harness is model-agnostic and ready; 32 fixture labels cannot fit any of them → **P10**, needs P1 |
| §12 | Feature engineering, four categories | **PARTIAL** | Structured and semantic features exist inside `score.py`; nothing is extracted as a reusable feature vector, so no model can consume them → **P10** |
| §16 | Full initial-fit breakdown | **PARTIAL** | `matching/rubric.py` gives named dimensions 1–5; the spec's percentage-per-dimension format is not produced |
| §35 | Explainable ranking | **HAVE** | `rubric.py` plus `reasons_json` on every Match |
| §36 | Capture user feedback | **HAVE** | `Match.decision` via `/swipe` |
| §36, §38 | **Feed feedback into ranking** | **BUILD** | Nothing reads `decision` to rank. It drives batch tailoring and a calibration display only → **P3** |
| §37 | Outcome labels | **PARTIAL** | `Outcome` enum and inbox routing populate them; no model consumes them → **P9**, needs P1 |
| §44 | Ranking tiers with labels | **BUILD** | Scores are raw floats end to end; no tier band → **P8** (small) |
| §48 | Selection on latency and explainability, not accuracy | **HAVE** | `benchmark.py` records ms/item and refuses to name a winner on noise |
| §57 | Model drift monitoring | **BLOCKED** | Needs a production model to drift. After P10 |

### Résumé and truthfulness (spec §10–§11, §15, §18–§24, §29–§30, §34, §51, §54)

| Spec | Capability | Status | Notes |
|---|---|---|---|
| §10 | Candidate master profile | **HAVE** | `Profile`, `Resume`, `Project` models |
| §11 | Never fabricate | **HAVE** | `tailor/guard.py`, `tests/test_no_fabrication.py` is a merge gate |
| §11, §34 | FACT / INFERENCE / MISSING / UNVERIFIED classification with confidence | **BUILD** | The guard is binary — supported or refused. Nothing labels a claim's evidentiary status or attaches confidence → **P5** |
| §15 | Transferable-skill reasoning | **PARTIAL** | The guard correctly refuses "knows FastAPI" from Flask experience. It cannot express "may transfer", so transferable evidence is discarded rather than recorded → **P5** |
| §18 | Choose the best base résumé of several | **BUILD** | Every path reads `profile.base_resume_id`. With three résumés the other two are unreachable → **P2** (small) |
| §19–§22 | Targeted rewrite, bullets, ATS and keyword optimization | **HAVE** | `tailor/rewrite.py`, `bullets.py`, `keywords.py` |
| §23 | ATS score before and after | **HAVE** | `tailor/ats.py`, shown on `/review` |
| §29 | Résumé version database | **HAVE** | `Resume.version`, `tailored_by`, content hashes |
| §30 | Original vs optimized diff | **PARTIAL** | `tailor/diff.py` renders the diff; changes are not classified ADDED/REMOVED/REWORDED/REORDERED/EMPHASIZED and carry no per-change rationale → **P5** |
| §51 | Hallucination test | **HAVE** | Gate 3 |
| §54 | Truth over ATS score | **HAVE** | The guard runs before anything is uploadable |

### Recruiter and strategy (spec §17, §24–§25, §27, §52–§53, §60–§64)

| Spec | Capability | Status | Notes |
|---|---|---|---|
| §17, §24 | Recruiter score, before and after | **BUILD** | Nothing exists. `ats.py` measures machine parse, which is a different question → **P4** |
| §52 | Four-level recruiter simulation | **BUILD** | Part of **P4** |
| §25, §53 | Application readiness score and gate | **BUILD** | No composite readiness score, no READY gate → **P8** |
| §27 | Adversarial verification agent | **BUILD** | The guard checks fabrication only. Nothing challenges seniority mismatch, keyword stuffing, contradictions, or inflated scores → **P13** |
| §60–§62 | The three user-facing report formats | **PARTIAL** | `/review` shows résumé, diff, ATS score, cover letter. Missing the match breakdown, recruiter score, readiness, and risks → falls out of **P4** + **P8** |
| §63 | Perspective switching between phases | **PARTIAL** | Separate modules exist; no orchestration runs them as distinct passes |

### Application flow (spec §31–§33, §42–§43, §49–§50)

| Spec | Capability | Status | Notes |
|---|---|---|---|
| §31 | Full preview before submit | **PARTIAL** | `/review` covers most of it; missing the scores P4 and P8 add |
| §32, §50 | Human approval before submit | **HAVE** | `AUTO_SUBMIT=false` default, state machine in `core/state.py`, tested |
| §33 | Sensitive questions flagged, never invented | **HAVE** | `ats/screen.py` knock-outs and cautions; §2.2 verbatim copying |
| §41 | `experiments`, `model_versions`, `training_examples`, `job_versions`, `recruiter_reviews` tables | **BUILD** | Experiment records are dataclasses written to stdout; nothing persists → **P14** |
| §42 | API surface | **PARTIAL** | Most routes exist under different names. Missing `/jobs/{id}/analyze`, `/jobs/{id}/resume-comparison`, `/feedback` |
| §43 | Dashboard sections | **PARTIAL** | 12 pages exist. No model-performance view → part of **P14** |
| §49 | Test coverage across the listed areas | **HAVE** | 1036 tests |
| §55–§56 | Code quality, observability | **PARTIAL** | structlog throughout; no metrics aggregation or crawler success-rate view |

---

## Buildable projects

Ordered by dependency. **P1 unblocks the most and is the least glamorous.**

Each names what exists to reuse, because in this repo the answer is usually
"more than you'd think".

---

### P1 — The labeling loop 🔑
**Spec:** §2, §4, §58, §59 · **Size:** M · **Blocks:** P3, P9, P10, and every ML claim

The one thing standing between this system and honest ML numbers. 32 fixture
labels for one synthetic profile cannot support any trained model, and
`docs/ML_EVALUATION.md` refuses to report a production candidate because of it.

**Build:** a `/label` screen that serves a real crawled posting and takes a
0–3 grade, writing to `seeds/labeled_matches.yaml` with `provenance: owner`.
Then active learning (§59): serve the postings the scorer is least certain
about first, so 100 labels buy more than 100 random ones would.

**Reuse:** `matching/labels.py` (schema, provenance, leak-safe splits) is
built and tested. `/swipe` already serves postings one at a time — this is
that interaction with a four-point scale instead of two.

**Done when:** ≥100 postings carry `provenance: owner`, and
`make bench-matching` stops printing the fixture-only blocker.

---

### P2 — Pick the right base résumé
**Spec:** §18 · **Size:** S

Every path reads `profile.base_resume_id`. Upload a data résumé, a backend
one and an ML one, and two of them are unreachable — the spec's exact
complaint about blindly using the newest.

**Build:** score each of the candidate's résumés against the posting, select
the closest, and record which and why on the application.

**Reuse:** `matching/score.py::score_posting` already scores text against a
posting. This is that function in a loop plus an `argmax`.

**Done when:** a candidate with three résumés gets the closest one selected
per posting, and `/review` names the base résumé and the reason.

---

### P3 — Feedback into the ranking
**Spec:** §36, §38 · **Size:** M · **Needs:** P1 for validation

`Match.decision` has been captured since `/swipe` shipped and **nothing ranks
with it**. It drives batch tailoring and a calibration chart. The owner's
skips are the cheapest signal in the system and they are being thrown away.

**Build:** a personalization layer over the base score — learned company,
technology and role preferences from past decisions, combined explicitly
rather than folded into the cosine.

**Reuse:** `benchmark.py` takes this as one more `Variant`, so you get the
control, the intervals and the tie reporting for free. Keep the base score
separate per `rubric.py`'s reasoning: a personalized score that silently
replaces the cosine invalidates Gate 5.

**Done when:** the personalized variant beats `production` on held-out
owner-labeled data with non-overlapping intervals. If it doesn't, that is a
result — report it and stop.

---

### P4 — Recruiter simulation
**Spec:** §17, §24, §52 · **Size:** M

The largest missing evaluation axis. `ats.py` asks "can a machine parse
this"; nobody asks "would a person shortlist this". The spec wants four
levels: 10-second scan, 30-second qualification, hiring-manager review,
technical credibility.

**Build:** `packages/tailor/recruiter.py`, scoring before and after
tailoring. Two constraints that decide whether this is worth anything:

- **The evaluator must not know which document it is reading** (§45).
  Compare the pair blind or the after-score is self-congratulation.
- **Deterministic signals first.** `tailor/evaluate.py` explains at length
  why an LLM judge drifts and makes a bad regression gate. Structure,
  burial depth, evidence density and credibility can be measured; reach for
  a model only for what cannot.

**Done when:** the score moves on documents a person agrees are better, and
a rewrite that only stuffs keywords scores *worse*.

---

### P5 — Provenance and confidence for every claim
**Spec:** §11, §15, §30, §34 · **Size:** M

The guard is binary — supported, or refused. The spec wants four states
(FACT / INFERENCE / MISSING / UNVERIFIED) with confidence and evidence.

The payoff is §15: Flask experience is a FACT, "may transfer to FastAPI" is
an INFERENCE, and "knows FastAPI" is neither. Today the guard correctly
refuses the third and has nowhere to put the second, so transferable
evidence is discarded instead of recorded.

**Build:** a claim record carrying status, confidence and the source lines.
Feed it into the diff so each change says what it targets and which fact
supports it (§30).

**Reuse:** `tailor/guard.py::SourceCorpus` already traces claims to source
lines — this promotes its verdict from a bool to a graded status.
`tailor/evidence.py` is the same idea for GitHub projects.

**Done when:** every bullet on `/review` shows its status and its source,
and no INFERENCE is ever rendered as a résumé fact.

---

### P6 — Canonical jobs and version history
**Spec:** §39, §40 · **Size:** M

The same job on Greenhouse and an aggregator is two rows. `content_hash`
detects that *one* posting changed; nothing merges two postings that are the
same job, and a changed posting overwrites its own history.

**Build:** a canonical job entity keyed on company + normalized title +
requisition + description similarity, and a `job_versions` table so
"requirements changed" is answerable.

**Reuse:** `crawler/extract.py` computes content hashes; `legitimacy.py`
already compares sibling postings, which is most of the similarity work.

**Done when:** one job posted on two boards appears once in the feed, and a
salary change is visible as a diff.

---

### P7 — Full job normalization
**Spec:** §6, §9, §13 · **Size:** M

`Posting` holds roughly a third of the spec's schema. Missing: salary range,
required vs preferred skills split, parsed education, parsed minimum
experience, work-authorization and sponsorship text.

That split is the interesting part, not the columns. §13 wants MANDATORY /
PREFERRED / OPTIONAL / AMBIGUOUS, and it changes filtering: a missing
preferred skill should cost ranking, a missing mandatory one should exclude.
Today `filters.py` treats every requirement it recognizes as hard.

**Reuse:** `crawler/extract.py` per-ATS extractors; `matching/filters.py`
already parses seniority and sponsorship from free text.

**Done when:** "5+ years required" excludes and "Kubernetes preferred" only
lowers, with the distinction visible in `rubric.py`.

---

### P8 — Readiness score and tiers
**Spec:** §25, §44, §53, §62 · **Size:** S–M · **Needs:** P4

The composite the spec puts in front of every approval, plus the quality
gate before an application may be marked READY, plus the tier labels
(95–100 Exceptional, 90–94 Very Strong, …) that make a raw float legible.

Small once P4 exists, because readiness mostly composes scores that other
projects produce. Build it after, not before — a readiness score over two
inputs is a rename, not a score.

**Done when:** `/review` shows all four scores with the tier, and an
application with a failing mandatory requirement cannot reach READY.

---

### P9 — Learn from outcomes
**Spec:** §37 · **Size:** M · **Needs:** P1

`Outcome` is populated by inbox routing and no model reads it. The spec's own
caution is the hard part: a rejection is not proof of a bad match, so these
are weak labels and treating them as ground truth will teach the ranker to
avoid competitive jobs.

**Done when:** outcome-derived labels are weighted below owner labels, and
the ablation showing they help is in the experiment record.

---

### P10 — The model bake-off
**Spec:** §3, §12, §46 · **Size:** L · **Needs:** P1

Logistic regression, random forest, XGBoost, LightGBM, CatBoost, a neural
ranker, a cross-encoder, LambdaMART, and the staged hybrid of §46 —
benchmarked against each other and against `jaccard`, which currently ties
the shipped scorer at 1/60th the latency.

**Blocked, not unstarted.** The harness is model-agnostic: a trained ranker
is a `Variant` with a `score` callable and inherits the control, the
intervals and the refusal to overclaim. What's missing is labels (P1) and a
reusable feature-vector extractor (§12) — today the features are computed
inline in `score.py` where no model can reach them.

Adding sklearn/xgboost to `requirements.txt` needs the owner's sign-off per
CLAUDE.md §3. They are free and local, so §11's no-paid-API rule is not in
the way.

**Done when:** the table in `ML_EVALUATION.md` has trained rows, and
whichever wins does so on held-out owner labels with a stated margin.

---

### P11 — Multi-category taxonomy
**Spec:** §8 · **Size:** S–M

`roles.py::canonical` returns one role. The spec wants a job in several
categories at once — Analytics Engineer is both DATA and SOFTWARE.

Read `roles.py`'s docstring before starting: the curated table is deliberate,
and the argument for it (Data Engineer and Data Scientist read as identical
to any embedding) applies to whatever replaces it.

---

### P12 — More ATS adapters
**Spec:** §5 · **Size:** M each, fully parallel

SmartRecruiters, iCIMS, SuccessFactors, Oracle Recruiting, Jobvite. The
cleanest project on this list: `packages/ats/base.py` is a stable protocol,
there are four worked examples, and CLAUDE.md §10 asks for one adapter and
one test file each. No dependency on anything above.

Workday stays out per CLAUDE.md §11.

---

### P13 — Adversarial verifier
**Spec:** §27, §64 · **Size:** M · **Needs:** P4, P5

An agent rewarded for finding problems: hallucinated experience, unsupported
technologies, seniority mismatch, keyword stuffing, contradictions, inflated
scores. It must be able to fail the pipeline, or it is decoration.

Needs P4 and P5 first — most of what it should check is not yet expressed
anywhere it could read.

---

### P14 — Experiment persistence and the model-performance view
**Spec:** §41, §43, §47, §56 · **Size:** M

`ExperimentRecord` is a dataclass printed to stdout. The spec wants
`experiments`, `model_versions` and `training_examples` tables, and a
dashboard section showing them.

Worth doing after there are experiments worth keeping — which means after
P1 and P10. Building the table first is the wrong order.

---

## Not building

| Spec | Why |
|---|---|
| §5 Workday | CLAUDE.md §11 — revisit after Phase 6 |
| §5 CAPTCHA bypass | CLAUDE.md §2.5 — hard boundary. A blocked site fails as `manual_completion_required` |
| Any paid API | CLAUDE.md §11 |
| Cloud deployment, multi-tenancy, billing | CLAUDE.md §11 — localhost, one user |
| Auto-submit without approval | CLAUDE.md §2.3 and spec §32 agree. `AUTO_SUBMIT=false` stays the default |

---

## If you want a starting point

**P12** if you want something self-contained with a clear finish line and no
dependencies — pick one ATS and follow `greenhouse.py`.

**P2** if you want the smallest real gap: an hour or two, and it fixes a case
where uploaded résumés are silently unreachable.

**P1** if you want the one that matters most. Nothing else in the ML half of
this spec can be honestly claimed until it exists.
