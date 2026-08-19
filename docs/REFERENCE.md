# References

Teardowns of outside projects solving an overlapping problem, kept so the
reasoning behind what we borrowed — and what we refused — survives the session
it happened in. One section per project.

---

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

## 7. santifer/career-ops

A second peer project, reviewed 2026-08-19: <https://github.com/santifer/career-ops>
(MIT), by Santiago Fernández de Valderrama, who used it across 740+ listings.
Same three ATSes as us, ~100 pre-configured companies, and — like Job Scout
and Talvora — it evaluates and drafts but never submits. That is now four
independent projects landing on §2.3's approval gate.

Taken:

- **Block G — legitimacy, kept out of the score.** Their strongest structural
  idea: the numeric score measures *fit*, a separate tier measures whether the
  posting is real. Mixing them lets a well-written ghost job rank highly
  *because* it is well written. This matters more here than there: they only
  draft, so a human reads every posting before anything is disclosed, while we
  auto-fill a real phone number and work-authorization answers into forms.
  `packages/matching/legitimacy.py` implements the six of their fourteen
  signals that are computable without an LLM or a web lookup.
- **Liveness verification** (their `scan.mjs --verify`), which named a gap
  discovery had just created — registry postings close by absence when the
  board is re-read, aggregator postings had nothing checking them ever.
  `packages/crawler/liveness.py`, and `UNKNOWN` never closes anything.
- **Untrusted-input discipline.** They treat imperative language in a JD aimed
  at the model as an anomaly to quote and continue past. We pass
  `description_raw` straight into the tailoring prompt and anyone can post a
  job; the guard already defends against it structurally, since output must
  trace to the résumé. That held by construction rather than by intent, so it
  is now pinned by tests in `tests/test_no_fabrication.py`.

Still open from this source:

- **Funnel analysis** (their `analyze-patterns.mjs`). We hold
  `ApplicationEvent`, `outcome`, `outcome_at` and `Match.score` and never ask
  whether a higher score actually correlates with a reply. That is the
  feedback loop §3.5 says is missing.
- **Their ~100-company portal list**, MIT and reusable with attribution,
  against our 29.
- **Follow-up cadence** — nothing currently says "applied 14 days ago, silent".

### Reviewed again 2026-08-19, against the owner's auto-apply goal

The repository was cloned and read directly this time — 464 `.mjs` files, 79
providers under `providers/`, MIT. Three findings, all checked rather than
inferred.

**Their apply step is weaker than ours, and deliberately so.**
`prepare-application.mjs` says it "prints a prefill summary to stdout. Never
POSTs anything — the user reviews the output, opens the apply URL, and submits
themselves." The README is blunter: *"It never submits, sends, or clicks
anything"*, and *"Does career-ops auto-apply to jobs for me? No."* We drive a
real browser, fill the live form, and screenshot it. Their handoff is text;
ours is a filled form plus a rendered PDF. This is the one axis where we are
clearly ahead, and it is worth stating because the numbers — 65k stars, ~400
contributors — invite the opposite assumption.

**Their VC-portfolio seeding does not port to us, and the blocker is §2.6.**
`seeds/vc-portfolios.mjs` walks `api.ycombinator.com/v0.1/companies` (248
pages × 25 = ~6,200 companies) and the a16z portfolio page, then probes each
company's ATS. It is the single biggest coverage idea in the project — ours
seeds 29 companies. It is also unusable here:

| Host | `/robots.txt` | Verdict |
|---|---|---|
| `api.ycombinator.com` | `User-Agent: * / Disallow: /` | refused |
| `www.ycombinator.com` | `Disallow: /companies?*` | refused |
| `www.workatastartup.com` | `Disallow:` (open) | allowed, but 302s to a login |
| `a16z.com` | 404 — no file | permitted; HTML scrape of one VC |
| `api.smartrecruiters.com` | `LinkedInBot Allow: /v1/companies/`, then `* Disallow: /` | refused |
| `api.recruitee.com` | `Disallow: /` | refused |

So the compliant remainder is a16z alone, by HTML scrape. §2.6 is a
non-negotiable and their project does not carry the equivalent rule, which is
why the same code is fine there and not here. Worth knowing when comparing the
two CLIs side by side: theirs will surface YC companies and ours will not, and
that gap is a rule, not a missing feature.

**Reading their robots handling sent us to check our own, and ours was
wrong.** Not wrong in their direction — wrong in the strict direction.
`api.ashbyhq.com` answers **401** for `/robots.txt`, and this project treated
every non-404 4xx as unreadable and refused. One of four extractors was
crawling nothing, silently, and it read as an ordinary skip in the crawl
report. RFC 9309 §2.3.1.3 makes any 4xx "unavailable" — the crawler "MAY
access any resources" — and reserves the MUST-disallow for 5xx (§2.3.1.4).
Fixed; a live re-check returned 137 open postings from a board that had been
returning `Blocked`. The 5xx half is unchanged, which is the half that
matters.

Not taken: the AI-coding-CLI-as-runtime architecture, the Go/Bubble Tea TUI,
and markdown/YAML/TSV as the datastore. Their archetype routing (role-specific
scoring weights) is interesting and was left alone as complexity a single
owner with one profile does not need yet.

---

## Credits

The bulk of this document came from **Shirin Khosravi Jam** and her
*Observable Job Agent* project, and §7 from **Santiago Fernández de
Valderrama**'s *career-ops*.

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

---

# Reference — GiraffyReach

**Source.** <https://www.giraffyreach.com> — a commercial real-time job
discovery and recruiter-outreach product. Reviewed 2026-08-18.

**Provenance caveat, and it is a large one.** Only the marketing site was read.
There is no repository, no API docs, and no technical write-up. The
"how it works" page describes outcomes and discloses no mechanism: nothing
about how portals are enumerated, which ATSes are integrated, or how
submission actually happens. Every number below is a claim of theirs, quoted,
and none of it is verified. Where a statement about *our* code appears it was
checked by running our code.

This is the closest thing to a commercial version of what this project is, so
it is worth reading as a map of the problem rather than as a source of designs.

---

## 1. What they claim to be

Four steps, in their words:

1. **Direct indexing** — *"Our crawler hits 106,000+ company career portals
   every hour, around the clock."* Workday and Greenhouse named explicitly, and
   framed against aggregators rather than alongside them.
2. **Real-time feed** — postings surface *"before they hit the major job
   boards"*, an asserted *"2–6 hour"* lead over LinkedIn and Indeed, with
   *"updates within 60 minutes of post"* as the stated SLA.
3. **First-mover application** — apply in their UI, or "C2C Autopilot", which
   tailors the résumé and sends.
4. **Tracking and outreach** — a "Universal Application Tracker", and
   "MCP Connect" so Claude or ChatGPT can search and match against the feed.

Filters they sell: saved searches, eight industry sectors, contract vs
full-time. Pricing $0 / $19.99 / $39.99 / $69.99 a month, the top tier being
*"up to 100 applications a day, sent for you"*.

---

## 2. The useful confirmation

**They index ATS portals directly, not aggregators — and say so as a selling
point.** That is the shape this project already has, so the crawler is not the
thing to rethink.

What differs is inventory: 106,000 tenant slugs against our 29. Their moat is
not the crawler, it is the *list*. Read that way, `packages/crawler/discover.py`
is aiming at the right target — promotion turns each resolved posting into a
registry entry, so the list grows itself rather than being written up front.
Mobile's aggregator sources are the bootstrap for that list, not a competing
strategy, and this reference is the argument for keeping both.

The open question they do not answer: how 106,000 slugs were enumerated in the
first place. Nothing on the site says. Our promotion loop is the slow, honest
version of whatever that was.

---

## 3. Adopt

### 3.1 Freshness as a tracked number

Their entire pitch is a time delta — *"within 60 minutes"*, *"2–6 hours ahead"*.
We measure nothing like it. `Posting.first_seen_at` records when *we* saw a
posting and nothing records when it was *published*, so "are we late?" is
currently unanswerable.

Greenhouse's board API returns `updated_at` per job and Lever's returns
`createdAt`. Storing the source timestamp alongside `first_seen_at` makes the
lag computable, and lag is the number that says whether `poll_interval_s` is
set sensibly. Cheap, and it turns a guess into a measurement.

### 3.2 Saved filters, separate from the profile

They sell saved searches and sector filters as a first-class input. We derive
every filter from the `Profile` row — `apply_filters(profile, posting, ...)`
reads location, sponsorship and clearance off it, and the only caller-supplied
knobs anywhere are `min_score` on `/matches` and a text `q` on `/postings`.

That conflates two different things: *what I want to see* and *what goes on my
application*. Changing your profile to stop seeing junior roles also changes
what gets typed into a form. They are separate concerns and should be separate
models.

### 3.3 Poll cadence as a product decision

They poll hourly. Our seeds carry `poll_interval_s: 21600` — six hours — and
that number was never argued for. It is defensible when the floor was 60s per
host and every Greenhouse board shared one; §2.6's amendment removes that
constraint for shared ATS APIs, so the cadence should be revisited on purpose
rather than inherited.

---

## 4. Considered, not adopted

**Sector taxonomy.** Eight fixed categories — Software, Electrical, Civil,
Pharma, Manufacturing, Biotech, Mechanical, Healthcare. Coarser than cosine
scoring over an embedding, so it buys nothing for ranking.

It would buy something for *cost*: a cheap pre-filter that drops most postings
before embedding them. Not worth building at 29 companies. Worth revisiting if
promotion grows the registry by an order of magnitude, which is the point of
promotion.

---

## 5. Rejected, and why

### 5.1 "Up to 100 applications a day, sent for you"

This is the product's headline capability and it is out of scope here by
design — §2.3 requires explicit approval before anything submits, and §11 rules
out volume submission entirely.

It is also, on the evidence we have, not straightforwardly possible. Running
`make gate-1-live` against real boards on 2026-08-17 found **all three ATSes
mount a captcha at the apply stage** — Greenhouse (Figma, Vercel), Lever, and
Ashby, each confirmed independently. Getting to a hundred submissions a day
through those forms requires either solving captchas or an access path the
public forms do not offer.

The site says **nothing** about captcha, rate limiting, or bot detection. For a
crawler striking 106,000 portals hourly and an autopilot sending a hundred
applications a day, that silence is the most informative thing on it.

§2.5 is a scope boundary, not a missing feature. This reference does not change
that; it clarifies what is on the other side of it.

### 5.2 Tiered delay as a mechanic

The free tier is deliberately 24 hours stale. A single-user local tool has no
tier to sell and nobody to withhold freshness from.

### 5.3 Recruiter outreach

"Outreach" in their name and tracker implies contacting recruiters directly.
Nothing in §9 covers it, the inbox is ingest-only by design, and adding an
outbound path to real people is a scope decision for the owner rather than an
increment.

---

## 6. If someone picks this up later

Ordered by value:

1. **§3.2** — a saved-search / filter model distinct from `Profile`, applied at
   query time in `/matches` and `/postings`, surfaced on the match feed. This is
   the one the owner described as the main idea of the project, and the spec's
   §1 still describes the narrower "curated list" version.
2. **§3.1** — store the source-published timestamp beside `first_seen_at` and
   report the lag. One column and one number; it makes cadence arguable.
3. **§3.3** — revisit `poll_interval_s` now that the shared-host floor is 2s.
4. **§4** — sector pre-filter, only once the registry is large enough for
   embedding cost to matter.

---

---

## Credits

**Shirin Khosravi Jam** — *Observable Job Agent* / "Job Scout",
<https://github.com/jamwithai/observable-job-agent> (MIT). §1–§6 above.

**Santiago Fernández de Valderrama** — *career-ops*,
<https://github.com/santifer/career-ops> (MIT). §7 above. His Block G is the
fuller version of `packages/matching/legitimacy.py`, and his `--verify` flag
named a gap we had just built into discovery ourselves.

No code was copied from either repository into this one. What was taken is
design reasoning, credited here rather than absorbed silently. Both projects
are MIT-licensed and worth reading in full — each is a better piece of
engineering than this summary of it.

GiraffyReach is a commercial product with no public source. Nothing was taken
from it but the reading — what a competitor's marketing honestly suggests about
our own architecture, including the parts we will not build.
