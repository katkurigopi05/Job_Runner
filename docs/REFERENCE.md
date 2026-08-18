# Reference — Job Scout / observable-job-agent

Notes taken from an outside project that solves an overlapping problem, kept so
the reasoning behind what we borrowed (and what we refused) survives the
session it happened in.

**Source.** *Observable Job Agent* / "Job Scout" by Shirin Khosravi Jam —
<https://github.com/jamwithai/observable-job-agent> (MIT), written up in the
*Build your own Job Agent* series on <https://jamwithai.substack.com>.
Reviewed 2026-08-17 against the `main` branch.

**Provenance caveat.** `jamwithai.substack.com` is blocked by this sandbox's
egress proxy, so the prose posts were not read directly. Everything below comes
from the repository itself — `README.md`, `src/job_scout/validation.py`,
`docs/phase3_findings.md`, the tree listing — plus search-result summaries of
the posts. Where a number is theirs it is attributed; where a claim about
*our* code appears it was verified by running our code, and the check is shown.

---

## 1. What the two projects share, and where they diverge

Job Scout is a **search-and-rank** agent: CV in, ranked postings out, tailored
materials on request. Its own README states the boundary — *"The human applies.
The agent never submits."* That is our §2.3 arrived at independently, which is
worth knowing: the approval gate is not us being timid, it is what the second
team building this also concluded.

The divergence is scope. Job Scout ends where its ranking ends. Jobrunner's
expensive half — driving the real ATS form, the §6 state machine, the review
queue, the tracker — has no counterpart there. So the parts of their design
worth taking cluster in tailoring, validation, and observability, and almost
nothing in orchestration transfers.

| Their concept | Our counterpart | Verdict |
|---|---|---|
| `corpus_ref` per-bullet provenance | `packages/tailor/guard.py` (corpus-wide) | **adopt** — closes a real gap, §3.1 |
| Prompt versions on the trace | `packages/llm/audit.py` | **adopt** — §3.2 |
| Model budget in settings | none | **adopt** — §3.3 |
| Named source failures | `CrawlReport.blocked` / `.failed` | mostly have it; one gap, §3.4 |
| Deterministic validator, no LLM judge | `guard.py` is already deterministic | agreed independently |
| Measured fabrication rate on live data | fixtures only (§15) | **adopt the practice** — §3.5 |
| Single-metric prompt optimization | n/a | **adopt the warning** — §3.6 |
| Concurrent source fan-out | rate-limited serial crawl | not applicable, §4.1 |
| LangGraph memory checkpoint | Postgres queue + §6 state machine | **reject**, §5.2 |
| "CV on trace" | deliberately excluded | **reject**, §5.1 |
| JSearch / OpenAI / Groq | free tiers + Ollama | **reject**, §5.3 |
| Gradio, ElevenLabs voice, Three.js | Next.js dashboard | out of scope, §5.4 |

---

## 2. Their fabrication validator, in detail

Worth recording precisely, because it is the part of their system closest to
our §2.1 merge gate and it is built differently.

- Zero LLM calls. Comparison is `difflib.SequenceMatcher` over normalized text.
- Normalization lowercases, strips punctuation, collapses whitespace, and
  canonicalizes units — `"10M"` → `"10 million"`, `"/day"` → `"day"`.
- Three tunable thresholds, environment-configurable, **recorded in every
  report** so a result can be reproduced:

  | Surface | Setting | Default |
  |---|---|---|
  | CV bullets | `fab_bullet_ratio` | 0.65 |
  | Skills | `fab_skill_ratio` | 0.85 |
  | Cover letter | `fab_letter_ratio` | 0.55 |

- Skills pass on **subset containment** — `"AWS"` is allowed when the corpus
  says `"basic AWS"`, on the principle that claiming less is honest.
- Cover-letter sentences are only checked when they are *factual* (contain a
  digit, a year, or a capitalized multi-word name). Motivational sentences are
  skipped rather than flagged.
- A sentence that fails gets a second pass against pairwise combinations of the
  top three references, because a true claim often blends two corpus items.

Their measured rates: fabrication **0.2768 → 0.1288** across their phases; the
validator improvements themselves reclaimed only ~6% of flags, and their own
note is that *"the bulk of the flag load is real rewrite drift"* — the validator
was not the main problem, the prompt was.

---

## 3. Adopt

### 3.1 Per-bullet provenance (`corpus_ref`) — the one that matters

**Their design.** Every tailored bullet carries a `corpus_ref` pointing at the
specific source item it derives from. Validation resolves that reference and
compares the rewrite *against that item only*. An unresolvable ref is itself a
violation.

**Our design.** `SourceCorpus.from_texts()` builds one flat token set over the
whole résumé. `check()` asks whether each extracted entity appears *anywhere*
in that set. `vet()` adds a per-bullet floor, `vocabulary_overlap >= 0.35`.

**The gap.** Because the corpus is flat, an entity belonging to one employer
satisfies a bullet written about a different employer. The overlap floor does
not catch it, because the rewrite can keep most of the original's wording and
still import a foreign metric. This is attribution fabrication: every fact is
individually true, and the claim as written is false.

Verified against our code as it stands:

```python
resume = """
Acme Corp - Backend Engineer
Maintained the billing service and reduced invoice errors.
Globex Inc - Data Engineer
Built a streaming pipeline processing 40TB per day across 12 regions.
"""
corpus = SourceCorpus.from_texts(resume)

original  = "Maintained the billing service and reduced invoice errors."
candidate = ("Maintained the billing service, reduced invoice errors "
             "across 12 regions processing 40TB per day.")

vet(original, candidate, corpus)
# -> accepted: True | reason: None
# -> entities checked: 2 | violations: 0
```

Globex's throughput is now on the Acme bullet, and the guard approves. On a
real application under the owner's name, that is exactly the outcome §2.1
exists to prevent.

**What to change.** Give `SourceCorpus` per-item structure instead of one token
set, have the rewrite path carry the id of the bullet it started from, and scope
the entity check to that item plus a shared global section (contact details,
skills list) rather than the whole document. Widening from item scope to
document scope should be an explicit, named decision — the same way
`SourceCorpus`'s own docstring already says widening the corpus should be
"done deliberately and never quietly."

This deserves a test in `tests/test_no_fabrication.py`; the case above is the
test.

### 3.2 Prompt versions in the audit trail

Their Opik setup records prompt versions alongside traces. Our
`packages/llm/audit.py` records timestamp, provider, task, `left_machine`,
SHA-256 of system and user prompts, and character counts — but nothing that
identifies *which template* produced a given output.

The consequence is that when tailoring quality moves, the trail cannot say
whether the prompt changed or the model did. Their §3.6 finding below is the
exact situation where that matters.

A version identifier is metadata, not content, so this sits inside both §2.8
and §10 — it adds no copy of the résumé. Concretely: a module-level constant
per template, bumped when the template is edited, carried into `AuditEntry`.

### 3.3 Model budget in settings

Their config carries a model budget; they report ~$0.37 for a 15-case tailoring
batch and "a few dollars end to end."

Cost is not our exposure — §3 mandates free tiers and §11 bars paid APIs — but
**quota** is. Gemini's free tier is request-capped per day, and one tailoring
pass across a full match feed can spend it. We cap `max_tokens` per call in
`packages/llm/provider.py` and nothing across calls.

The cap should fail closed: park the application and surface the reason, rather
than silently degrading to a weaker provider mid-résumé. Silent downgrade would
put two different quality tiers in one document with nothing recording it.

### 3.4 An empty parse is not an empty board

Their principle: *"a source that returns nothing says why"* — quota, auth,
timeout — rather than returning an empty list indistinguishable from no results.

We already do most of this. `crawl_company()` separates `Blocked`, fetch
exceptions, and non-200 status into `CrawlReport.blocked` / `.failed`, and
`board_hash` short-circuits unchanged boards.

The gap is downstream of a *successful* fetch. On HTTP 200 with a body we
cannot parse — Greenhouse changes their JSON shape, or a degraded board serves
`{"jobs": []}` — `extractor.parse()` returns `[]`, and `_close_missing()` then
marks **every open posting for that company closed**, because nothing it knows
about appears in an empty `seen` set.

Ours is the more damaging version of the failure they describe: theirs returns
no results, ours destroys state. A company that had 30 open postings yesterday
and parses to zero today is far more likely a parser break than a mass
closure. The fix is a floor — do not close in bulk when a parse yields zero and
the company previously had open postings; record it as a suspect result on the
`CrawlReport` and leave the postings alone.

### 3.5 Measure fabrication on real material, not fixtures

`docs/phase3_findings.md` reports fabrication rates on *live* job data and gates
regressions on them: a deterministic revalidation gate runs in ~3s against
stored packs and fails if fabrication exceeds 0.27.

CLAUDE.md §15 already admits our Gates 5 and 6 pass against fixtures we wrote
rather than the owner's own material. Their setup is the shape of the answer:
keep the fixture suite as the fast regression gate, and add a stored pack of
real postings crossed with the real résumé, reported as a *rate* rather than
pass/fail. A rate is what tells you a prompt edit made things worse; a boolean
gate only tells you when it made them catastrophic.

### 3.6 One metric is not enough — their most useful finding

Recorded almost verbatim because it is the kind of thing that is cheap to read
and expensive to rediscover.

They optimized their tailor prompt against the deterministic fabrication metric
using a reflective optimizer, 5 trials × 12 samples, 92 LLM calls. Objective
`1 - fabrication_rate` improved 0.772 → 0.868; offline confirmation showed
fabrication 0.309 → 0.1423. A real win on the metric being optimized.

On the same test packs, Opik's built-in hallucination judge went **up 64%**.
Their own words: the optimizer *"wrote to precisely what it was asked to and
moved a metric nobody was watching."* The hypothesis is that tighter paraphrase
requirements pushed the model toward constructions the judge flags. Both scores
had been sitting in their telemetry the whole time — *"the gap was our own
notes, not our telemetry."*

The lesson transfers directly. If we ever tune `MIN_VOCABULARY_OVERLAP`,
`MAX_GROWTH_RATIO`, or the tailoring prompt against `guard.py`'s pass rate, we
will be optimizing against the one referee we control, and a rewrite can satisfy
the guard while reading worse to a human. Any tuning pass needs a second
measure — readability, or owner judgement on a sample — reported next to the
first, before the change is kept.

---

## 4. Considered, not adopted

### 4.1 Concurrent source fan-out and the two-phase soft deadline

They parallelized their source cascade (3.01s → 2.01s) and, when JSearch
reliably burned its full 15.3s timeout returning nothing, added a two-phase soft
deadline: let the fast source return at ~1.0s, consume the slow one only
opportunistically. Median search time went 15s → 1s.

Good engineering, no purchase here. §2.6 puts a **60-second minimum** between
requests to the same host, and our crawl is one board per company rather than
several sources racing for one answer. Shaving seconds off a fetch that is
followed by a mandatory 60-second wait optimizes the wrong term.

The transferable fragment is narrower: a slow source that reliably yields
nothing should be demoted rather than waited on. If a seed company's board
times out repeatedly, that belongs in the dead-board reporting `validate.py`
already does.

### 4.2 `uv` and a committed lock file

They use `uv` with a committed `uv.lock`. We use pip and `pyproject.toml` with
no lock.

CLAUDE.md §13 exists because every dependency defect this project has had was
invisible locally and only appeared on a clean install — `email-validator`,
`python-docx`, `python-multipart`, `mcp`, `pyyaml`. A lock file is aimed at
exactly that class. Not urgent, since CI already installs from nothing on every
commit, which catches the same failures a step later. Worth revisiting if CI
ever starts breaking on a transitive upgrade we did not make.

---

## 5. Rejected, and why

### 5.1 "CV on trace"

Their observability attaches the CV to the trace. For us this is a direct
violation of §2.8 and §10: it uploads the owner's résumé to a third-party
observability service, and `packages/llm/audit.py` was written specifically to
avoid that trade — it records digests and sizes so the owner can prove what left
the machine without the trail becoming a second copy of the résumé.

Not a close call, and not a thing to soften later for convenience.

### 5.2 LangGraph with a memory checkpoint

Their orchestration is a LangGraph agent with a checkpoint, including a
`reformulate_query` loop capped at 2 iterations.

Ours is a Postgres queue with `FOR UPDATE SKIP LOCKED`, the §6 state machine,
and an append-only `ApplicationEvent` log where `transition()` is the single
write point. For an agent that fills real forms under one person's name, that is
strictly the better property: every status change is durable, attributable, and
inspectable after the fact. Swapping in a graph checkpoint would trade an audit
trail for a framework.

The reformulation loop has no analogue at all — their discovery is query-driven,
ours is registry-driven over ~50 curated companies in `seeds/companies.yaml`.
There is no query to reformulate.

### 5.3 JSearch, OpenAI, Groq

JSearch is metered RapidAPI; OpenAI and Groq are paid. §11 bars paid APIs
without asking the owner first. Adzuna and Remotive have free tiers and are
technically eligible, but they pipe in an uncurated firehose, which is the
opposite of the curated-registry design in §9 Phase 5. Raising that is a scope
conversation, not an implementation detail.

### 5.4 Gradio, ElevenLabs voice, Three.js console

We have the Next.js dashboard (§3). Their voice console is a well-built thing
aimed at a different product; §11 puts the equivalent surface out of scope.

---

## 6. If someone picks this up later

Ordered by value, all independent of each other:

1. ~~**§3.1** — per-item corpus scoping in `packages/tailor/guard.py`.~~
   **Done.** `SourceCorpus` now carries `items` and `shared`;
   `SourceCorpus.from_resume()` splits experience and projects into one item
   per entry, `vet()` scopes each rewrite to the entry its bullet came from,
   and `GuardReport.scope_ref` records which item was used — so a
   document-wide check is never mistaken for a narrow one. The repro above is
   `test_metric_from_another_employer_is_rejected`.
2. ~~**§3.4** — refuse to bulk-close postings on a zero-yield parse.~~
   **Done.** `_close_missing()` returns `(closed, suspect)` and declines to
   close anything when a clean fetch parses to nothing while open postings
   exist. The board hash is deliberately not recorded in that case, so the
   next cycle re-reads instead of short-circuiting on "unchanged" and going
   quiet. Surfaced as `CrawlReport.suspect`.
3. ~~**§3.2** — prompt version in `AuditEntry`.~~ **Done.**
   `packages/llm/prompts.py` holds each prompt with a name and version, keyed
   by digest so `record()` labels a call without anything having to pass the
   version down. The pinned digests in `tests/test_llm_prompts.py` are what
   make the version honest: editing a prompt without bumping it fails there.
4. ~~**§3.3** — daily provider quota that fails closed.~~ **Done.**
   `packages/llm/quota.py`, counted from the audit trail rather than a second
   tally that could disagree with it. Local providers are unlimited. Exceeding
   it raises before the request leaves, rather than falling back to a local
   model and putting two quality tiers in one résumé.
5. **§3.5 / §3.6** — read together before any tuning pass, not after.

---

## Credits

Everything in this document that is worth anything came from **Shirin Khosravi
Jam** and her *Observable Job Agent* project.

- Repository: <https://github.com/jamwithai/observable-job-agent> (MIT)
- Write-up: *Build your own Job Agent*, parts 1–4 —
  <https://jamwithai.substack.com>
  - [Part 1](https://jamwithai.substack.com/p/build-your-own-job-agent-part-1)
  - [Part 2](https://jamwithai.substack.com/p/build-your-own-job-agent-part-2)
  - [Part 3](https://jamwithai.substack.com/p/build-your-own-job-agent-part-3)

Two debts in particular are worth naming, because in both cases her work found
a defect in ours rather than merely suggesting an improvement:

- **`corpus_ref`** — per-bullet provenance is the idea that exposed the
  attribution hole in our fabrication guard (§3.1). We had been checking that
  every fact was *true somewhere* and calling that grounded. It isn't.
- **Publishing the numbers that went the wrong way** (§3.6). It costs nothing
  to report a 53% improvement and stay quiet about the judge that moved 64% in
  the opposite direction. She reported both, and that is the single most useful
  paragraph either of us will read this year.

No code was copied from her repository into this one. What was taken is design
reasoning, which is credited here rather than absorbed silently. Her project is
MIT-licensed and worth reading in full — it is a better piece of engineering
than this summary of it.
