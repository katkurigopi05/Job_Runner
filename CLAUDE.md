# CLAUDE.md — Jobrunner

Project context for Claude Code. Read this before any task in this repo.

---

## 1. What we are building

**Jobrunner** — a local, single-user job-application agent. It finds job postings
matching filters the owner sets, from any company's careers page it can reach, scores them
against the owner's profile, rewrites the résumé per posting, fills the real ATS form in a
headless browser, and holds for approval before submitting. Recruiter replies are ingested
and routed back to the application record.

**"Any company" is a direction, not a claim.** The registry in `seeds/companies.yaml` is a
seed, not the boundary. Discovery ingests postings from aggregators, resolves each to a
real ATS form where it can, and promotes the boards it finds into the registry so the
crawler polls them first-hand from then on (`packages/crawler/discover.py`). The list grows
itself from what it finds rather than being written up front. A commercial product in this
space indexes 106,000 career portals; we index what we have promoted so far, and the
mechanism that closes that gap is promotion, not a bigger hand-written file. See
`docs/REFERENCE.md`.

**Filters are the owner's input, not a reading of their profile.** What the owner wants to
see and what goes on their application are different things, and conflating them means
narrowing a search also changes what gets typed into a form.

Reference teardown of the commercial product this is modeled on: `docs/TSENTA_ARCHITECTURE.md`.

**Runs entirely on localhost. Zero recurring cost. One user: the repo owner.**

---

## 2. Non-negotiable rules

These are correctness requirements, not preferences. Violating any of them is a bug.

1. **Résumé tailoring never invents facts.** Experience rewriting may rephrase, reorder,
   re-emphasize, and inject keywords *that are already supported by that résumé entry or a
   shared source section*. It may not borrow a project skill into an employer bullet. The
   Projects section may add facts verified by GitHub's source-reported repository name,
   description, primary language, and topics, and must keep them attributed to that project.
   It may not add a skill, employer, date, credential, or metric absent from those sources.
   There is a test suite for this (`tests/test_no_fabrication.py`) and it is a merge gate.
2. **Work-authorization and employment-history answers are copied verbatim from the profile.**
   Never LLM-generated. These have legal consequences for the applicant.
3. **Nothing submits without explicit approval by default.** `AUTO_SUBMIT=false` is the
   shipped default. Auto-submit is opt-in per profile and requires a match-score threshold.
4. **Unanswerable question parks the application.** Status becomes `needs_review` and the
   exact question text is surfaced. Never guess, never leave blank, never fabricate.
5. **No captcha-solving services, no bot-detection evasion, no residential proxy rotation.**
   When a site blocks automation, the application fails as `manual_completion_required` and
   the owner finishes it by hand. This is a hard scope boundary.
6. **Crawler respects robots.txt and rate limits.** Minimum 60s between requests to the same
   host. Configurable up, never down.

   **Amended:** a host that is *known* to be a multi-tenant ATS API — one endpoint
   serving thousands of companies' boards, listed explicitly in
   `ratelimit.SHARED_API_HOSTS` — has its own floor of 2s instead. The original rule
   pictures a company's own careers page, where 60s is courtesy. Applied to
   `boards-api.greenhouse.io` it protects nobody: it serializes every company in the
   registry behind one counter, capping the crawler at 60 boards an hour regardless of
   how many are listed. What makes this a narrowing rather than a loophole: the host
   list is explicit so nothing is promoted by resembling it, the 2s floor is refused
   below exactly like the 60s one, a site's own `Crawl-delay` still raises its host's
   delay, and a `429`/`Retry-After` backs that host off for as long as it asks. Polling
   faster is only defensible while also listening.
7. **Secrets never touch the database in plaintext and never appear in logs.** ATS account
   passwords go through `packages/core/vault.py` (Fernet, key from OS keychain or `.env`
   outside the repo). `.env` is gitignored and stays gitignored.
8. **Résumés are PII.** Local storage only. No third-party upload except the LLM call needed
   for tailoring, and that call is logged so the owner can audit what left the machine.

---

## 3. Stack (all free)

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.12 | services + workers |
| API | FastAPI + Pydantic v2 | routes mirror the reference API shape |
| DB | Postgres 16 + pgvector | Docker, localhost:5432 |
| Queue | Postgres `FOR UPDATE SKIP LOCKED` | no Redis — one less service |
| Browser | Playwright (Python, Chromium) | persistent context per ATS in `./storage/browser/` |
| LLM | provider abstraction, see §7 | Ollama local / free tiers / Claude Code |
| Embeddings | `sentence-transformers` BAAI/bge-small-en-v1.5 | local, CPU, 384-dim |
| Storage | `./storage/` behind an S3-shaped interface | swap to S3 later without code change |
| Frontend | Next.js 15 + Tailwind v4 | `next dev`, localhost only. Components are hand-rolled, not shadcn — see below |
| Mail | IMAP poll, Gmail `+alias` addressing | `owner+app{id}@gmail.com` |
| Migrations | Alembic | |
| Tests | pytest + pytest-asyncio | |
| Lint/format | ruff + ruff format | |
| Types | mypy strict on `packages/core` and `packages/ats` | |

No paid service is permitted in `requirements.txt` or `package.json` without asking the owner first.

The frontend line originally said shadcn/ui. It was built without it, and the
row now says so. The components are small and few, the theme is ported wholesale
from another project of the owner's, and a generator that writes files into the
tree buys little when there are a dozen of them. Recording the deviation matters
more than the choice: a stack table that describes something the repo does not
contain is worse than either option.

Postgres runs on **5433** on the owner's machine, not the 5432 in the row above:
another project holds 5432. The remap lives in an uncommitted
`docker-compose.override.yml`, and `.env` points at 5433 to match.

`next dev` binds **0.0.0.0:3001**, not the localhost:3000 the row implies. Port
3001 because 3000 is taken; `0.0.0.0` so the dashboard can be read from the
owner's phone on the same LAN, which is how a review queue gets checked away
from the desk.

This is a real narrowing of §1's "runs entirely on localhost", and it is worth
naming rather than burying. What §1 is protecting is that no résumé, no
recruiter thread, and no vault secret leaves the owner's control — and binding
a dev server to the LAN does widen who can reach that surface. It is defensible
only on a trusted network, and only for `dev`: `next start` is unchanged and
still binds localhost, so nothing about a non-dev run is affected. On an
untrusted network — a café, a conference, shared housing — set it back. The
dashboard has no authentication, because until now it never needed any.

---

## 4. Repo layout

```
jobrunner/
├── CLAUDE.md
├── docker-compose.yml            postgres only
├── Dockerfile                    verification artifact, not a deployment
├── .github/workflows/ci.yml      runs the gates on a clean machine
├── .env.example                  never .env
├── apps/
│   ├── api/                      FastAPI app
│   │   ├── main.py
│   │   ├── routers/              candidates, profiles, applications, postings, usage
│   │   └── deps.py
│   ├── worker/                   queue consumer
│   │   ├── run.py                poll loop
│   │   ├── apply_job.py          the apply pipeline
│   │   └── crawl_job.py          career-page polling
│   ├── mcp/                      MCP server exposing tools to Claude Code
│   └── web/                      Next.js dashboard
│       └── assistant             local chat over the owner's own data, §14
├── packages/
│   ├── core/                     models, schemas, state machine, vault, storage, queue
│   ├── ats/                      adapters — base.py + one file per ATS
│   ├── crawler/                  fetchers, change detection, posting extraction
│   ├── tailor/                   résumé rewrite, diff, PDF render, fabrication guard
│   ├── matching/                 embeddings, hard filters, scoring
│   ├── inbox/                    IMAP ingest, classification, routing
│   ├── llm/                      provider abstraction, task router, audit trail
│   ├── analytics/                funnel, cadence, weekly digest — reads only, never writes
│   └── github/                   the owner's own repos, behind /projects
├── migrations/                   alembic
├── storage/                      gitignored — resumes, PDFs, screenshots, browser profiles
├── tests/
└── docs/
    ├── TSENTA_ARCHITECTURE.md    teardown of the commercial reference
    ├── REFERENCE.md              what the teardown implies for this build
    ├── PARITY.md                 capability map against career-ops
    ├── BACKLOG.md                gap register against the job-discovery spec
    ├── ML_EVALUATION.md          what the ranking numbers may and may not claim
    └── PARALLEL_WORK.md
```

---

## 5. Data model

Authoritative definitions live in `packages/core/models.py`. Summary:

```
User(id, email, created_at)

Candidate(id, user_id, name, email, email_mode[managed|self],
          managed_alias, secrets_ref, created_at)

Profile(id, candidate_id, label, base_resume_id, phone, location,
        work_auth, needs_sponsorship, links_json, salary_expectation,
        answers_kv_json, min_match_score, auto_submit, created_at)

Resume(id, candidate_id, version, storage_ref, parsed_json, is_default)

Company(id, name, domain, careers_url, ats_type, poll_interval_s, last_polled_at)

Posting(id, company_id, ats_type, external_id, url, title, location,
        description_raw, description_embedding vector(384),
        content_hash, published_at, first_seen_at, closed_at)

Match(id, profile_id, posting_id, score, reasons_json,
      decision, decided_at, tailored_resume_id, created_at)

Application(id, candidate_id, profile_id, posting_id, url, ats,
            status, failure_reason, review_json,
            tailored_resume_id, cover_letter_ref, receipt_json,
            outcome, outcome_at, created_at, updated_at)
        UNIQUE(candidate_id, url)

ApplicationEvent(id, application_id, type, payload_json, at)   -- append-only

InboundMessage(id, candidate_id, application_id, from_addr, subject,
               body, classification, link_method, link_confidence, at)

Project(id, candidate_id, source, external_id, name, full_name, url,
        homepage, description, language, topics_json, stars, forks,
        is_fork, is_archived, is_private, pushed_at,
        include, pinned, synced_at, created_at)

QueueTask(id, kind, payload_json, status, attempts, run_after,
          locked_at, locked_by)
```

Indexes: `Posting(first_seen_at DESC)`, `Posting(content_hash)`, ivfflat on
`description_embedding`, `Application(status)`, `Match(profile_id, score DESC)`,
`QueueTask(status, run_after)`.

---

## 6. Application state machine

```
queued ──> running ──> submitted        (terminal, success)
             │  ▲
             │  ├── needs_review ──approve──> running
             │  │                └──reject──> failed[rejected_at_review]
             │  └── needs_otp ────otp────────> running
             └──────────────────────────────> failed  (terminal)
```

`failure_reason` ∈ `job_closed`, `unsupported_site`, `incomplete_candidate`,
`manual_completion_required`, `rejected_at_review`, `site_error`.

Transitions go through `packages/core/state.py::transition()` only. It validates the
edge, writes an `ApplicationEvent`, and is the single place status changes. Direct
assignment to `Application.status` anywhere else is a bug.

---

## 7. LLM provider abstraction

`packages/llm/provider.py`:

```python
class LLMProvider(Protocol):
    async def complete(self, system: str, user: str, *, max_tokens: int) -> str: ...
    async def complete_json(self, system: str, user: str, schema: type[BaseModel]) -> BaseModel: ...
```

Implementations: `OllamaProvider`, `OllamaCloudProvider`, `GeminiProvider`,
`AnthropicProvider`, `StubProvider`.
Selected by `LLM_PROVIDER` env var. `StubProvider` returns deterministic canned output and
is what tests use — no test may hit a network LLM.

Task routing (`packages/llm/router.py`):

| Task | Default provider | Temp | Why |
|---|---|---|---|
| classify inbound email | Ollama | 0.0 | cheap, local, easy; one word from a fixed set |
| map form field to profile key | Ollama | 0.0 | structured, low creativity |
| tailor résumé | best available | 0.3 | quality matters most here |
| write cover letter | best available | 0.7 | the one task where variance buys something |
| answer open-ended question | best available | 0.3 | goes on a real application |

Temperature is routed per task for the same reason the provider is — these
tasks want opposite things from a model. Left unset they run at each vendor's
default, around 1.0, which is wrong for nearly all of them. Tailoring in
particular is bounded by the fabrication guard: a creative model there does
not write better résumés, it raises the rejection rate and falls back to the
original bullet, which looks like the tailorer doing nothing.

The table lives in `packages/llm/router.py::TEMPERATURES`. `complete_json` is
pinned to 0.0 regardless — the answer has to parse against a schema.

**Local or cloud is now the owner's choice, per task.** The provider column
above was not settable: `best_available()` walks a hardcoded `QUALITY_ORDER`
and picks the strongest *configured* provider, so a Gemini key made every
"best available" task remote and the only way to tailor locally was to delete
the key that everything else wanted kept. `LLM_TASK_TAILOR`,
`LLM_TASK_COVER_LETTER` and `LLM_TASK_OPEN_ENDED` take `auto` (the shipped
behaviour) or a provider name.

Only those three. Classification and the assistant read recruiter mail and
chat context; §2.8 permits one third-party upload and neither is it, so both
stay on Ollama in code and `CHOOSABLE_TASKS` does not list them — a setting
able to move them would be a way to opt out of a non-negotiable by editing
`.env`. `test_only_the_uploading_tasks_are_choosable` holds that.

The assistant has since gained a cloud option, and it is worth being precise
about what that did and did not change here. It is **still absent from
`CHOOSABLE_TASKS`**, and `/chat` still ignores `LLM_PROVIDER`. The owner picks a
provider per question in the UI (§14); no environment variable redirects the
assistant, which is the property this list protects. Inbound-email
classification did not move at all.

**OpenRouter is a fourth provider, and it is opt-in by name.** One key reaches
many upstream models, and `docs/PARITY.md` had it refused under §3's "no paid
service without asking" — the owner asked, and the route in use is free, so §11
is untouched. What is *not* untouched is §2.8. OpenRouter forwards the résumé
text to an upstream provider, and the audit trail can record the hop but not the
destination; on a cloaked `stealth/*` route the upstream vendor is undisclosed
by design, and free routes commonly log prompts and share them with that
undisclosed creator. So `OpenRouterProvider` is deliberately absent from
`router.QUALITY_ORDER`: a key in `.env` changes nothing on its own, and the
provider answers only when named by `LLM_PROVIDER` or one of the three settings
above. Acquiring a route whose recipient cannot be named should take typing the
word, not pasting a key.

The endpoint has one property worth knowing before pointing anything at it:
**reasoning is mandatory and cannot be switched off.** Both `{"enabled": false}`
and `{"max_tokens": 0}` come back `400 "Reasoning is mandatory for this endpoint
and cannot be disabled"`. That matters because `max_tokens` there bounds
reasoning *plus* answer, while every caller here means it as an answer budget —
`tailor_bullets` passes 300 to keep a bullet bullet-sized. Passed through
unchanged, a 300-token call returned `finish_reason="length"` with **empty
content**: the allowance went on thinking and the model was cut off before
writing. The tailorer caught the error and kept the original line, so the
symptom was a tailorer that appeared to do nothing on a provider that was
working fine. `REASONING_HEADROOM_TOKENS` is the fix, and the numbers behind it
are in the constant's docstring.

Free routes also rate-limit hard: a three-bullet résumé trips 429 at the default
`LLM_CALL_INTERVAL_S=4.0`. The pacer obeys `Retry-After` and retries, but for a
real batch raise the interval.

`LLM_FALLBACK_LOCAL` answers with the local model when the daily allowance is
spent or the remote provider is unreachable, rather than refusing. This is not
a softening of "nothing falls back to the stub" — that stands, and the stub is
excluded explicitly. `QuotaExceeded` already told the owner to "raise the
limit, wait for the reset, or run a local provider"; the third option was an
instruction to a human, and this is it automated. Which model answered is
recorded on the provider and in the trail, because a résumé tailored by
llama3.1 after the allowance ran out is a different document from one tailored
by Gemini and the owner approving it should be able to tell.

"Should be able to tell" was, until now, only true of someone reading the
trail. `answered_by` lived on the provider object and died with the run, so the
review screen — the one place the distinction has consequences, because it is
where the document gets approved and sent — never showed it. It is now stored
on the résumé as `resumes.tailored_by` and rendered under the diff.

On the résumé rather than the application, because the reuse paths are the ones
that would otherwise go blank: an overnight batch and the tailoring cache both
serve a document written in an earlier run to an application that never calls a
provider, and the model is a property of the document, not of the run that
attached it. Read *after* the rewrites too, not before — `FallbackProvider`
resets `answered_by` to the primary at the top of every call, so a value
captured early names the model that did not answer, which is precisely the case
worth seeing.

NULL means unrecorded — a base résumé, or one tailored before the column
existed — and the screen says "not recorded" rather than showing nothing. A
blank line reads as "no fallback happened", which is the one thing it must not
be mistaken for. The migration deliberately does not backfill: there is no
record of which model wrote the existing rows, and a plausible guess on a
document about to be sent to an employer is worse than an honest gap.

The cover letter records the same thing, in the review record rather than on a
row. §7's fallback applies to `write_cover_letter` exactly as it does to
tailoring, so a letter written by llama3.1 after the allowance ran out is not
the letter Gemini would have written, and it goes to an employer under the
owner's name. A tailored résumé has a row to carry `tailored_by`; a letter has
none, so `answered_by` rides in `review_json["cover_letter"]` beside the text.
Refusals carry it too — "the guard refused it" reads differently depending on
which model produced the draft.

The review screen shows the letter itself, open by default. It had been written
into `review_json` since the wiring landed and rendered nowhere, so a document
generated by a model was attached to applications the owner approved without
ever being able to read it — the same failure the untailored résumé had, and
invisible for the same reason: no test asserted what the approval screen
displays.

### Comparing two models on one posting

`/review` has a **Compare models** panel: it tailors the same posting with the
local model and with the cloud one and shows both, each with its rewrite count,
its guard-refusal count, and a button that makes it the document to upload.

It exists because §7 made the provider settable without making it *decidable*.
Answering "is the cloud one better for this résumé and this job" meant editing
`.env`, re-running, and holding the first result in your head.

Three properties are load-bearing:

- **On demand.** Each remote side is another §2.8 upload of the owner's résumé.
  Running both on every application would double that on every application,
  including the ones rejected at review. The tailoring cache is consulted per
  provider, so comparing a posting twice sends nothing.
- **Both sides are guard-checked before either is shown.** A comparison offers
  each column as something the owner may choose and send. An unvetted draft
  presented that way is a fabricated bullet with a button under it. The refusal
  counts are shown per side — a model that keeps trying to invent should not
  look identical to one that does not.
- **A side that cannot run is a reported column, not a missing one.** No key,
  spent allowance, Ollama not started: each becomes a candidate carrying the
  reason. A comparison silently missing half of itself reads as a verdict on the
  half that is there. `CannotCompare` is separate and covers the case where
  there was never anything to compare — no base résumé, no posting text —
  because a precondition is not a model outcome.

Selecting is restricted server-side to the two versions actually compared. This
sets the file an employer receives, and "any résumé id this candidate owns" is a
wider door than the screen needs.

**It did not always set that file.** Selecting wrote `tailored_resume_id` and
approving then re-entered `_tailor`, which reattached the default provider's
cached document over it — so the column the owner picked was shown here and the
other one was sent. The choice is pinned now; see §15, *The owner's choice at
review was discarded on the way to the employer*.

**The remote side is nameable per comparison.** `cloud` on the compare request
picks it for that run only; omitted, it is whatever real tailoring would use.

This exists because the default could not answer the question for OpenRouter.
§7 keeps `openrouter` out of `QUALITY_ORDER`, so the only way to make it the
cloud column was `LLM_TASK_TAILOR=openrouter` — which also redirects every real
tailoring call. The owner had to *adopt* a provider in order to evaluate it,
which is the friction this panel exists to remove, pointing the wrong way: the
provider hardest to name is the one whose output most deserves a look first.

It is not a back door into §7. Only a provider with a key configured may be
named, so pasting a key is still what makes a route reachable at all; nothing
routes to OpenRouter by default; and naming one here moves no setting, so the
next application tailors exactly as it did before. That is the same shape as
§14's per-question provider choice in `/chat`. The picker states where the
résumé goes for each option, and says outright that OpenRouter forwards to an
upstream it cannot name — §2.8 permits this upload, it does not excuse making
the recipient invisible at the moment of choosing.

**A provider that never answered is not a guard refusal.** Found on the first
real local-vs-cloud run. `tailor_bullet` keeps the original line for either
reason and set `rejected_reason` for both, so `summarize` counted them
together — and the comparison rendered "1 refused by the guard" for a model
that had returned a 404 and never written a word. On a screen whose entire
purpose is judging two models against each other, that is the wrong verdict
about the wrong subject: "the guard refused this" describes what a model tried
to write, and a transport error describes the network. `provider_failures` is
counted and displayed separately, and the column says so.

Tuning any of these against the guard's own pass rate is the trap in
`docs/REFERENCE.md` §3.6: it optimises the one referee we control, and a
rewrite can satisfy the guard while reading worse. Change one only with a
second measure reported beside it.

---

## 8. ATS adapter contract

`packages/ats/base.py`. Every ATS lives behind this and nothing ATS-specific leaks out.

```python
class ATSAdapter(Protocol):
    name: str

    @staticmethod
    def matches(url: str) -> bool: ...

    async def parse_posting(self, page: Page) -> ParsedPosting: ...

    async def enumerate_fields(self, page: Page) -> list[Question]: ...

    async def fill(self, page: Page, answers: dict[str, Any]) -> FillReport: ...

    async def submit(self, page: Page) -> Receipt: ...
```

`Question.kind` ∈ `text`, `textarea`, `email`, `phone`, `url`, `single_select`,
`multi_select`, `radio`, `checkbox`, `boolean`, `date`, `file`, `cover_letter`,
`typeahead`, `hidden`, `display`.

`FillReport` records every field filled, every field skipped, and the exact text of any
question that could not be answered. `Receipt` holds the field-by-field record plus a
screenshot path — this is the audit trail the owner reviews.

Adapter build order: **Greenhouse → Lever → Ashby → Workable**. Greenhouse first: no login,
stable DOM. Workday last or never — it needs per-employer accounts and is the hardest target.

---

## 9. Build phases

Each phase has a gate. Do not start the next phase until the gate passes. Run
`make gate-N` to check.

### Phase 0 — Skeleton

Build: docker-compose with Postgres+pgvector. Alembic migrations for the full schema.
FastAPI with `/candidates`, `/profiles`, `/applications` (create/list/get),
`/applications/{id}/review`. `packages/core/state.py` state machine. Postgres queue with
`SKIP LOCKED`. Worker loop that picks up a task, sleeps 2s, transitions to `submitted`.
Vault module. Storage module. `StubProvider`.

**Gate 0:** `pytest` green. `POST /applications` → poll `GET /applications/{id}` reaches
`submitted`. Invalid transition raises. Duplicate `(candidate_id, url)` returns 409.
`ApplicationEvent` rows exist for every transition. ruff + mypy clean.

### Phase 1 — First ATS, end to end

Build: Playwright worker with persistent browser context. `packages/ats/base.py` +
`greenhouse.py`. `POST /detect` (URL-pattern classifier). Real apply pipeline:
`parse_posting` → `enumerate_fields` → `fill` → screenshot → `submit`. `AUTO_SUBMIT=false`
default, so it parks at `needs_review` with the filled-form screenshot.

**Gate 1:** against a live Greenhouse posting, `enumerate_fields` returns the real field
list; `fill` populates them from a fixture profile; a screenshot lands in `storage/receipts/`;
application reaches `needs_review`. A recorded-HAR replay test runs the same flow offline in CI.

### Phase 2 — Profile and résumé

Build: résumé upload, parse to structured JSON (`packages/tailor/parse.py`), profile
answer store, field-to-profile-key mapping. Unmapped question → `needs_review` carrying the
exact question. Dashboard: profile editor + review queue with approve/edit/reject.

**Gate 2:** a real posting fills ≥80% of fields with zero manual input. Every unfilled field
appears in the review queue with its original question text. Approving in the UI resumes
the application.

### Phase 3 — Tailoring

Build: `packages/tailor/rewrite.py` — job description in, rewritten bullets out.
`packages/tailor/guard.py` — the fabrication check. Diff generation. PDF render (WeasyPrint
or ReportLab). Cover letter. Per-company caching of tailored versions.

**Gate 3:** `tests/test_no_fabrication.py` passes — for 20 fixture job descriptions crossed
with 3 fixture résumés, every noun-phrase entity in output traces to the source résumé.
Diff renders in the UI before send. Tailored PDF is valid and ATS-parseable
(round-trip through the parser and compare).

### Phase 4 — MCP server

Build: `apps/mcp/` exposing `search_postings`, `tailor_resume`, `detect_ats`,
`apply_to_url`, `review_queue`, `approve_application`, `application_status`.

**Gate 4:** Claude Code connects to the MCP server and drives a full apply-to-review cycle
conversationally. Do this phase early even though it looks like polish — it makes every
later phase testable by conversation instead of by curl.

### Phase 5 — Discovery

Build: company registry (hand-picked, seeded from a YAML file). Crawler with
content-hash change detection and per-host rate limiting. Posting extraction per ATS.
Embedding of postings and profile. Hard filters (location, seniority, work authorization,
sponsorship). Cosine scoring. Match feed in the dashboard.

The registry started at 50. `make validate-seeds` found that 21 of those
returned 404 from both the board API and the rendered page — those companies
have left Greenhouse — and they were removed, taking it to 29. A 404 board is
worse than an absent one: it yields zero postings, which reads identically to
"nothing new since the last poll".

**It is now 119**, and this paragraph said 29 until someone counted. The
import of career-ops' company list added 90 entries and superseded the number
without updating the sentence that carried it. Two things follow, and both
matter more than the count:

- The paragraph also claimed the 21 dead boards were "listed at the bottom of
  `seeds/companies.yaml` with the evidence rather than deleted". They were
  deleted. There is no retired section in that file and never has been. The
  argument for keeping them is still right; it was simply never implemented.
- **The 90 imported entries have never been validated.** The 404 sweep ran
  against the original 50. So the registry today is 29 checked boards and 90
  unchecked ones, and on the evidence of the first sweep — 21 of 50 dead —
  a meaningful share of the newcomers are polling nothing. Run
  `make validate-seeds` before trusting a quiet crawl.

**Gate 5:** crawler runs a full cycle over the seed list without exceeding rate limits;
second run emits zero postings (change detection works); match scores are sane against a
hand-labeled set of 20 postings — the ones you'd actually apply to rank in the top 10.

### Phase 6 — Tracker

Build: IMAP poll, `+alias` per application, inbound classification (interview / rejection /
info request / noise), auto status routing, pipeline board.

**Gate 6:** a test email to `owner+app{id}@gmail.com` lands on the right application and
moves its status. Classification is ≥90% accurate on 30 hand-labeled real recruiter emails.

---

## 10. Conventions

- Async everywhere in api/worker. No sync DB calls in request handlers.
- Pydantic models for every boundary. No bare dicts crossing a module edge.
- Errors use the shared envelope: `{"error": {"code", "message"}}`. Codes:
  `unauthorized`, `invalid_request`, `not_found`, `rate_limited`,
  `duplicate_application`, `invalid_state`, `internal_error`.
- Every queue handler is idempotent. At-least-once delivery is assumed.
- Commits: Conventional Commits, subject ≤50 chars. `feat(ats): add lever adapter`.
- One adapter per file. One test file per adapter.
- Structured logging (structlog). Never log secrets, résumé contents, or full page HTML.
- Selectors live in a `SELECTORS` dict at the top of each adapter, never inline — DOM
  changes should be a one-place fix.

---

## 11. Explicitly out of scope

Do not build these. If a task seems to need one, stop and ask.

- Multi-tenancy, user accounts beyond the single owner, billing, credit ledger
- Mobile apps, iMessage, WhatsApp
- Chrome extension (maybe later, not now)
- Captcha solving, proxy rotation, fingerprint spoofing
- Cloud deployment, and any hosted runtime. The app runs on localhost only.
  (CI is now in scope — see §13. It *runs* the gates, it does not deploy
  anything, and no image is pushed to a registry.)
- Workday adapter (revisit after Phase 6)
- Any paid API

---

## 12. Start here

```bash
mkdir jobrunner && cd jobrunner
git init
# place this file at ./CLAUDE.md and the teardown at ./docs/TSENTA_ARCHITECTURE.md
claude
```

Then, first prompt:

> Read CLAUDE.md. Build Phase 0 only. Start by writing the docker-compose, the Alembic
> migration for the full schema in §5, and `packages/core/state.py` with the state machine
> from §6 plus its tests. Show me the migration and the state machine before writing the
> API routes.

Work one phase at a time. Do not let it run ahead — every phase has a gate for a reason,
and a broken Phase 1 adapter is much cheaper to find than a broken Phase 5 pipeline built
on top of it.

---

## 13. CI

`.github/workflows/ci.yml` runs on every push to `main`, every pull request,
weekly, and on demand. Two jobs, and they do not both run on every trigger —
see `image` below.

**`gates`** — the same checks `make gate-N` runs locally, on a machine that has
nothing installed. It brings up `pgvector/pgvector:pg16` as a service container,
installs the native libraries WeasyPrint loads through cffi, installs Playwright
Chromium, applies migrations, then runs `make gate-0` followed by every other
gate by name.

Gate 0 already runs the whole suite, so the named gates are subsets re-run for
labelling. That is deliberate: when CI goes red you want to be told which gate
broke, not that "a test failed".

**A new gate has to be added here too.** The named steps are a hand-maintained
list, so a `gate-N` that exists in the Makefile but not in `ci.yml` still gets
*run* — gate 0 covers every test — but a failure in it reports as a gate-0
failure and you lose the label that says which phase regressed.

**`image`** — builds the `Dockerfile`, imports every entry point inside it, and
checks Chromium is present. Nothing is pushed anywhere.

It does **not** run on pull requests. The repo is private, so Actions minutes
are metered on the free plan, and this job is the expensive half of a run — it
downloads Chromium a second time to build the image (~3 of the ~7 billable
minutes). What it proves changes when the `Dockerfile` or the dependency set
changes, not on every branch push, so it runs on `main`, weekly, and on
demand. **A pull request that touches either should be checked by hand with
`workflow_dispatch` before merging** — that is the cost of this trade, and it
is on you to remember it.

### Why this exists

Every dependency defect this project has had was invisible locally and only
appeared on a clean install: `email-validator`, `python-docx`,
`python-multipart`, `mcp`, `pyyaml`. `python-multipart` is the instructive one —
nothing imports it, FastAPI reaches for it at runtime, so no amount of reading
the source finds it. Only installing from nothing does.

The native side is the same story. WeasyPrint needs Pango and Playwright needs a
browser; neither is a Python dependency, and a missing Pango does not fail
cleanly — it segfaults pytest partway through the run.

### The Dockerfile is not a deployment

There is no registry, no orchestrator, and no hosted runtime. §1 still holds:
this runs on localhost for one user. The image exists so "it installs from
nothing" is checked on every commit.

Two things in it are load-bearing and easy to undo by accident:

- **No `chown -R` over `/opt/playwright`.** Changing a file's mode rewrites it
  into a new layer, so recursing over a 1.3GB browser added 947MB of duplicates.
  Chromium only needs to be readable and executable. Root can own it.
- **`.dockerignore` excludes `.env`, `.secrets/`, and `storage/`.** Those are
  the vault key, the encrypted credentials, and résumé PII. An image layer is
  forever, so keep them out of the build context.

---

## 14. The assistant

`/chat` in the dashboard, and a dock on the review screen. A chat surface over
the owner's own data, answered by a model on this machine.

It was built after §9's phases and is not one of them. Three properties are
load-bearing and easy to undo by accident.

**Local by default; remote only when asked, per question.** This paragraph used
to read "local-only, not configurable", and that was the rule for good reason:
chat context carries application URLs, profile fields, and recruiter
correspondence, and §2.8 permits exactly one third-party upload — the tailoring
call — which a chat window is not.

**The owner widened it deliberately.** `ChatRequest.provider` accepts `ollama`,
`gemini`, `anthropic`, `openrouter` or `ollama_cloud`, and the `/chat` UI has a
picker. Naming a
remote provider sends the context to it. Recording the change rather than
quietly editing the rule away: this *is* a narrowing of §2.8, it was made
knowingly, and a reader who finds recruiter mail in a Gemini log should be able
to find out why here.

What did not change is what happens when nobody asks:

- Omitting `provider` answers locally. The shipped default still costs nothing.
- `apps/api/routers/chat.py` still ignores `LLM_PROVIDER` entirely, so nothing
  about tailoring configuration moves the assistant.
- The assistant is still **not** in `CHOOSABLE_TASKS`. The choice is a
  per-request field made in the UI for one question, not an environment
  variable that silently redirects every future conversation — the `.env`
  opt-out route §7 warns about stays closed.
- **No fallback in either direction.** A local model that is down does not get
  promoted to a cloud provider, and a cloud provider that fails does not drop
  to the local one. `LLM_FALLBACK_LOCAL` covers tailoring and deliberately does
  not reach here: there the fallback is recorded on the document, here it would
  be a different answer wearing the same label.
- **Inbound-email classification is untouched** and remains local in code. The
  owner widened the assistant, not the inbox.

The `audit.is_local` check survives, narrowed to the local path. Ollama serves
cloud-hosted models over the same localhost API — `kimi-k2.6:cloud` and
`qwen3-coder:480b-cloud` are both in the owner's model list, neither runs on
this machine, and the base URL is identical either way. That check was never
only about distance; it is about the label matching. Choosing Gemini openly is a
decision. Asking for the local model and silently getting a third party is not a
decision at all, so it still refuses.

The reply carries `model` and `local`, both computed rather than inferred from
the provider name, and the UI marks every remote turn. An answer that cost
privacy must never look like one that did not.

**Recruiter mail is gated separately, and defaults to withheld.** Choosing a
cloud provider does not take the inbox with it. `ChatRequest.share_mail` is
`False` unless the owner ticks the box for that question, and the `/chat` panel
shows the box only when a remote model is selected.

It is separate from the provider choice because it is a different decision. The
rest of the context is the owner's own material — their applications, their
profile. Recruiter correspondence is *other people's writing about them*, sent
privately, by people who never chose a provider. Consenting to send your own
data somewhere is not the same as consenting to send theirs, so the two are not
one switch.

For the local model it is always included and the toggle is not shown. The gate
is about crossing a boundary; on the side where nothing crosses there is nothing
to gate, and rendering a control that does nothing would be worse than
rendering none.

When it is withheld the context says so — `recent replies: withheld` — rather
than omitting the section. The assistant is told to answer from what it was
handed and to say when it does not know, so a missing section would read as "no
replies have arrived", which is a different answer and a wrong one.

The model is **llama3.1**, chosen by benchmarking the six local models against
this project's own tasks rather than by reputation. On the 30 labeled recruiter
emails behind Gate 6 it classified 30/30 where the next best managed 28; it
answers the assistant's probes from the context it was handed; and asked what
salary to request it points at the profile, where `qwen2.5-coder` instead
advised on how to research one — the §2.2 failure, from the model rather than
the route. It is also the tersest of the six, and `CHAT_SYSTEM` asks for brief.

Worth knowing what that benchmark did *not* prove. Of §7's five tasks only
`tailor_resume` has a caller; `classify_inbound_email`, `map_form_field`,
`write_cover_letter`, and `answer_open_ended_question` are defined in
`packages/llm/router.py` and called from nowhere, and `LLMClassifier` is never
constructed — the inbox is rules-only, deciding 29 of those 30 emails without
a model at all. The assistant is the one live local-model path, so it is the
one the choice was made on.

**§2.2 is refused before the model is reached.** Asked what to put for work
authorization, sponsorship, employment history, or salary, the route returns a
refusal and points at the profile. The system prompt says the same thing, but
the prompt is a request and this is a rule — the check runs in code.

**Grounded, not freehand.** The model is handed real counts, the actual
application, and its recent replies, and told to say when it does not know. An
assistant that invents an application status is worse than no assistant.

The audit trail in `packages/llm/audit.py` records every provider call —
digests and sizes, never the prompt itself. §2.8 wants proof of what left the
machine; §10 forbids logging résumé contents. Both hold: the trail proves what
was sent without becoming a second copy of it.

---

### Ollama's own hosted models

The owner asked for `glm-5.3-flash:cloud`. Recording how it landed, because the
obvious implementation was the wrong one and the difference is the whole point.

The obvious one is to let `OLLAMA_MODEL` take a `:cloud` tag. That is precisely
what the refusal above exists to stop: Ollama serves hosted models through the
identical `localhost:11434` API, so nothing in the request distinguishes them,
and a remote answer would carry the label `provider="ollama", local=true`.
Loosening the check would not have widened a choice, it would have removed the
owner's ability to know which they got.

So the remote path got **its own provider name**, `ollama_cloud`, and the
refusal is untouched — `test_asking_for_local_still_refuses_the_same_model`
runs the same model through both and asserts one answers and one is refused.
Naming it is the informed decision §14 was protecting; `OLLAMA_MODEL` still
means "a model on this machine".

Two consequences worth keeping visible:

- **`audit.is_local` is now right by construction rather than by substring.**
  Under `ollama` a call is presumed local and only "cloud" in the tag rescues
  the label; under `ollama_cloud` it is presumed remote. A retag cannot make
  the trail lie.
- **The provider refuses a *local* model**, which sounds like pedantry and is
  not. §2.8's trail is worth having only if it is read, and one that reports a
  résumé leaving when it did not teaches the owner to stop reading it. Both
  directions of mislabelling are refused, for one reason.

**It is deliberately absent from `router.QUALITY_ORDER`, and that matters more
here than it does for OpenRouter.** Every other remote provider needs an API
key, so an absent key keeps it out of "auto" on its own. This one needs no
credential once the local daemon is signed in — `_configured` is satisfied by
`OLLAMA_CLOUD_MODEL` naming a model. Were it in the quality order, that single
line in `.env` would send every "auto" task off the machine, with nothing in
the configuration that reads as having asked for it. §2.8 permits the upload;
it does not permit it happening unnoticed.

It is **not** in `CHOOSABLE_TASKS` either, so nothing about the list above
moved: the assistant is still chosen per question in the UI, never by an
environment variable.

Unlike an OpenRouter `stealth/*` route, the recipient is nameable — Ollama's
servers, running the tagged model — so the trail records where the résumé went
rather than only that it left. Both UI pickers say so at the moment of
choosing, and both say the quiet part: the address is localhost either way.

## 15. What the gates do and do not prove

Every gate in §9 passes. Two of them pass against fixtures rather than the real
material their wording asks for, and that distinction is worth keeping visible
where the gates are defined rather than only in a test docstring.

- **Gate 5** asks for "a hand-labeled set of 20 postings — the ones you'd
  actually apply to". `tests/test_matching.py` has 20, written the way real
  postings read. They are not postings the owner labeled.
- **Gate 6** asks for "30 hand-labeled **real** recruiter emails".
  `tests/test_inbox.py` has 30 written to match. They are not real correspondence,
  and the cost of that is now measured rather than suspected. The fixtures were
  written beside the patterns that read them, so they use the phrasing the
  patterns expect: the fixture says "with other candidates" and matches, while
  "with another candidate" — the same sentence as recruiters write it — does
  not. Six of seven realistic rejection phrasings miss. On a rejection shaped
  like one that actually arrives, the rules abstain outright.

  `inbound_messages` in the owner's database is **0**. Gate 6 has never seen a
  real email.

Both suites are worth having — they catch regressions. Neither answers the
question its gate was written to ask, which is whether the scoring and the
classifier work on *this owner's* material. Swap the fixtures for real data
before trusting either number.

- **Gate 3 now checks what it says it checks.** The wording is "every
  noun-phrase entity in output traces to the source résumé"; the guard matched
  on capitalization, acronyms and digits instead, which is a proxy that cannot
  see a lowercase claim. A rewrite adding "with machine learning" to a résumé
  that never says it was accepted with **zero entities checked**.
  `packages/tailor/chunk.py` POS-tags the output and extracts noun phrases, so
  nouns and their adjectives are claims while verbs and adverbs stay free —
  §2.1 permits rephrasing, and "oversaw" for "maintained" is rephrasing.

  The tagger data is a download, not a package: `make nltk-data`. Without it
  the guard falls back to the old proxy, `GuardReport.extractor` records which
  one ran, and `make doctor` reports it. A guard that quietly loses a check is
  worse than one that never had it, because nobody re-reads a green test.
- **Gate 5's ranking half is now measured rather than asserted.** The gate
  checks one bit — ten wanted postings in the top ten — which cannot see a
  match slide from rank 1 to rank 10 and cannot compare two scorers. It also
  turns out to prove less than it looks: on those twenty postings the shipped
  scorer and a five-line token-overlap baseline both score a perfect NDCG@10,
  because the negatives are pastry chefs and truck drivers and every scorer
  separates those. `make bench-matching` reports NDCG/MAP/MRR/P@K with
  bootstrap intervals against a constant control, over the same labels plus
  twelve adjacent roles that actually discriminate. On those twelve NDCG@5
  falls to 0.577 and the control is statistically tied with everything, which
  is where the matcher's real weakness lives. The labels are still
  fixture-grade, so the harness refuses to report a production candidate at
  all — `docs/ML_EVALUATION.md` says what would have to arrive first.

  Two side findings worth keeping. `seeds/labeled_matches.yaml` is now the one
  definition of the Gate 5 set, which `tests/test_matching.py` reads by tag —
  two copies of a labeled corpus drift, and the gate was the copy that would
  go stale. And `filters.seniority_ok` passes everything when
  `target_seniority` is unset, which no production caller sets: a Junior
  Backend Engineer with a perfect technology match ranks in the top ten, and
  arming the target takes P@10 from 0.900 to 1.000. Whether to arm it by
  default is a separate decision; it now has a number attached.

- **Gate 1's HAR replay** is now real. `tests/test_greenhouse_har.py` replays
  bytes Greenhouse actually served, offline, and runs in `make gate-1` and in
  CI. It exists because the hand-written fixture beside it had native
  `<select>` while the live board had moved to react-select, so the adapter
  misread every dropdown and the suite stayed green. Re-record with
  `python -m scripts.record_har <posting-url>`. A fixture drifts silently; a
  recording only goes stale, and stale is visible.

- **Gate 1's live half** has never completed. `make gate-1-live` reaches
  `parse_posting` and `enumerate_fields` against real Greenhouse boards and is
  then blocked by a captcha at `fill`. Per §2.5 that is the correct outcome, not
  a bug — but it means the fill path is proven only against a fixture.
- **Gate 2** needs a real posting and a real profile by definition.
  `make gate-2` checks the offline half; `make gate-2-live` is the other half.

### The tailored résumé was not the one being sent

Worth recording because every gate passed while it was true, and the code said
otherwise in a comment.

`apps/worker/apply_job.py` called `adapter.fill` — which uploads the file — and
*then* called `_tailor`. `_resume_path` read `profile.base_resume_id` and never
looked at `application.tailored_resume_id`. So Phase 3 ran in full on every
application: the rewriter, the guard, the project selection, the PDF, the diff
on the review screen — and the employer received the untailored base résumé.
The comment above the call claimed "the file the owner uploads is the document
the diff described", which is what a reader would have checked against.

Nothing failed, because no test asserted which path reaches the file input.
`tests/test_apply_uploads_tailored.py` now does, including the ordering itself:
`_tailor` must precede `adapter.fill` or there is nothing tailored to upload.

The fallback is deliberate and logged. If the tailored file is missing or
tailoring refused every rewrite, the base résumé is uploaded rather than none —
an honest untailored résumé beats an application with no résumé — but
`uploading_base_resume_tailored_file_unusable` is emitted, because sending the
base while the application record claims a tailored one is exactly how this
stayed invisible.

### Nothing told the owner an application was waiting

Three states park and all three were silent: `needs_review` wants an approval,
`needs_otp` wants a code, and `failed[manual_completion_required]` wants the
owner to finish the form by hand. The status was recorded and the dashboard
showed it, which on a queue whose entire promise is "nothing submits without
you" means the promise is kept only if the owner remembers to go and look.

`packages/core/notify.py` rings the doorbell. Three backends: `log` (the
shipped default, which goes nowhere), `desktop` (`notify-send` or `osascript`,
local and free), and `webhook` (the owner's own URL — ntfy, Telegram, Slack —
so a phone alert costs this project no dependency and no money).

**It is not a softening of §2.5.** The prompt that produced it recommended
integrating a CAPTCHA-solving service, which §2.5 and §11 both refuse and which
would also have been a paid API under §3. Nothing here defeats a challenge or
makes the browser look human. A blocked site still fails as
`manual_completion_required`; what changed is that the owner hears about it and
finishes it themselves, as a person, in their own browser. The work moved to a
human rather than around a control, and the notification links to the local
dashboard precisely so it cannot read as an automated retry.

Three details are load-bearing:

- **It fires after the commit, never inside `transition()`.** That function is
  the one place a status changes and would be the obvious hook, but it
  deliberately does not commit — a notification sent there announces an
  application that may still roll back. A doorbell that rings for work that did
  not happen is worse than a late one.
- **Idempotent on the reason, not the application.** The queue is
  at-least-once, so a re-run must not tell the owner twice; a `notified` event
  records delivery. Keyed on the reason so an application that parks, resumes
  and parks again *does* ring again — that is a second thing to do.
- **A failed backend never fails the application.** Every delivery is wrapped
  and logged by name. The exception body is not logged, because a webhook error
  can echo the URL and the URL can carry a token.

The webhook payload is an id, a status, the company and role, and a localhost
link. Never the résumé, the answers, or the posting body — §2.8 permits one
third-party upload and a notification is not it. `test_the_payload_carries_no_application_material`
asserts the exact key set rather than the absence of any one field, so adding
one is a deliberate act.

### The ATS score was reading the sales pitch

Found by running the guard and the ATS scorer over a rewritten résumé against
the real crawled postings in `tests/fixtures/golden/`, which is the first time
either had been pointed at one.

§7 makes the ATS keyword score the *independent second measure* — the referee
that is not the fabrication guard's own pass rate. On the Palantir Forward
Deployed Software Engineer posting it was measuring nothing. `job_terms` ranks
by frequency and keeps forty; in 5,912 characters of narrative, `Python`,
`Java`, `C++` and `TypeScript/JavaScript` appear exactly once each in a single
line naming the stack, while `world`, `problems`, `believe` and the company's
own name recur throughout. The top forty held every one of the latter and none
of the former, so a tailored résumé was being scored against a vocabulary
containing no skills at all — and the rewrite's measured gain was, correctly
and uselessly, zero.

The fix is in `ats._requirements_text`, which cuts to the posting's own
requirements section before the terms are counted. Across the twelve golden
postings, prose terms fell from **17% to 8%** of the vocabulary and the term
count *rose* (372 → 419): it is finding more real terms, not merely filtering.
On the worst posting it went 43% → 8%. One posting got worse (6% → 11%), which
is recorded rather than averaged away.

Two things about where the fix lives:

- **On the measurement side only.** `job_terms` output is also
  `TermReport.missing`, which `rewrite.vet` treats as vocabulary a rewrite may
  not borrow. Narrowing *that* removes words the model may then introduce
  unchallenged, so the guard rail is untouched and only the scorer's view of
  the posting changed. `ats.py` already drew that line; this stays behind it.
- **Headings rather than a technology list.** Ranking known technologies above
  prose was tried first and is worse twice over: a second skill vocabulary
  drifts against the alias table, and no list recognizes the skill it has not
  heard of — which is the term a posting is most worth reading for. All twelve
  golden postings carry a requirements heading. A posting without one falls
  back to being read whole, exactly as before.

### A Skills list moved technologies between employers

The same session, the same method. §2.1 says a rewrite may inject keywords
"already supported by that résumé entry **or a shared source section**", and it
separately forbids borrowing one entry's skill onto another. A Skills list makes
those two clauses contradict each other, and the second one was losing: every
technology named in Skills was indexed as shared, so it supported a claim on
*any* employer.

Scoped to an employer that never touched it, `Built Python services on
Kubernetes` passed while the Skills section existed and was correctly refused
when that section was deleted. Same claim, same scope, opposite verdicts,
decided by an unrelated part of the document.

`SourceCorpus.supports` now withdraws a shared token when some entry claims it.
The three cases, which are the whole design:

- **In this entry** — supported, as before.
- **In Skills only, claimed by no entry** — still supported anywhere. This is
  §2.1's shared-section allowance and it is deliberately untouched: a skill the
  owner lists but never attributed to a job can go anywhere.
- **In Skills *and* in another entry** — refused here. The résumé has already
  said where it belongs, and "the owner knows this" is not "the owner did this
  here".

On a two-job fixture it withdraws 13 of 34 shared tokens — the technologies —
and leaves 21, being contact details, education, and the skills the résumé
never attributed to anything. The full suite including Gate 3 passes unchanged,
so this refuses claims that were wrong rather than claims that were working.

### Phase 3 scope Gate 3 does not cover

§9 Phase 3 lists two things beyond what Gate 3 tests. Both are now built, and
both stay recorded here rather than being deleted: the gate still does not
cover either, so "Gate 3 passes" continues to mean less than "Phase 3 works".

- **Cover letter.** ~~Nothing calls it.~~ Wired. `apps/worker/apply_job.py::_cover_letter`
  writes one, `Application.cover_letter_ref` is written, and
  `tests/test_apply_cover_letter.py` asserts the wiring the module's own tests
  could not: that the letter is written *before* `adapter.fill`, and that it
  reaches the field. `tests/test_greenhouse.py` types it into the real fixture
  textarea, because a letter written, vetted, stored and then not typed is the
  same defect as the résumé that was never uploaded.

  Three things are deliberate. **The form is read first** — the questions
  decide whether a letter is written at all, because most postings never ask
  for one and a provider call for a field that does not exist buys nothing.
  **A refusal is recorded, not swallowed** — the guard offers no fallback
  here, so `review_json["cover_letter"]` carries the reason; a form that asked
  and got nothing otherwise looks identical to a form that never asked, and
  writing it by hand is the owner's call. **A resumed run reuses the stored
  letter** rather than writing a second one: any real provider returns
  different prose the second time, and the owner approved a specific letter.

  Wiring it surfaced a defect in the module that no test could see from
  inside. `cover.py` strips the greeting before sifting — `_GREETING` exists
  precisely so "Dear Hiring Manager," stops being read as a claim about a
  `Manager` — and then `write` joined it back on before calling `vet`, which
  runs the guard over the whole letter again. So the strip was undone one line
  later and the refusal came back. Every test in `test_cover_letter.py` built
  letters with no greeting, so all of them passed while the most common
  opening a model produces was refused on its first four words. `vet` now
  strips the addressing itself, which covers both callers. §2.2 and the length
  bound still judge the whole letter: the scaffolding is exempt from tracing
  to the résumé, not from the salary rule.
- **Caching of tailored versions.** Built — `packages/tailor/cache.py`. The
  condition it was waiting for arrived: `LLM_PROVIDER=gemini` with a key set,
  and tailoring text now leaves the machine. Keyed on the source résumé, the
  posting's `content_hash`, `TAILOR_SYSTEM.digest`, the attached project ids,
  and the provider and model — everything that changes the output, because a
  cache keyed on less serves a résumé written for a different job and nothing
  about it looks wrong. A posting with no `content_hash` is not cached at all
  rather than keyed on something weaker.

  Worth being honest about how much it currently saves: **very little.** With
  one profile, no two postings sharing a `content_hash`, `pending()` already
  skipping tailored matches, and `_prepared_resume` already reusing within an
  apply, there is no live path that re-tailors the same posting. It is
  insurance for when there are several profiles, and for a re-run after a
  partial failure.

  The measured duplication is somewhere else. The audit trail's heaviest day —
  204 uploads against a ceiling of 200 — carried only **69 distinct payloads**,
  and 189 of the 204 were `tailor.system`. Nothing persisted: the database
  holds one résumé and no tailored ones. That is `packages/tailor/evaluate.py`,
  an offline harness with no session, re-run over the same fixtures. This cache
  does not touch it and should not; a cache there is the follow-up that matches
  the evidence.

Gate 3 passed without either of them for as long as neither existed, because
it tests fabrication and the PDF round trip — the part with consequences — and
it still does not test either now that both are built. The cover letter has
its own tests rather than a gate. Recorded here so a green Gate 3 is not read
as more than it checks.

### The owner's choice at review was discarded on the way to the employer

The same defect as *The tailored résumé was not the one being sent*, one screen
further along, and it survived that fix because the fix asserted which résumé
`adapter.fill` uploads — not which résumé is still attached by the time it runs.

Approving a parked application does not resume mid-flight. It re-enters
`_run_pipeline` from the top: fresh page, re-`goto`, re-`enumerate_fields`,
re-`_tailor`. And every path in `_tailor` assigns `tailored_resume_id` — the
batch-prepared one, the cache hit, the fresh publish. So a decision the owner
made on the review screen was written to the row, and then quietly overwritten
by the run their approval started.

**Compare models** is where this had teeth. Choosing the cloud column set
`tailored_resume_id`; approving re-tailored with the router's *default*
provider, found that provider's cached document, and attached it instead. The
screen showed one document, the employer received another, and nothing on
either side reported the swap. §7 said "this sets the file an employer
receives", which is what a reader would have checked against.

`review_json["resume_pinned"]` is the fix, and `_tailor` returns before any of
the assigning paths when it is set. Two details are load-bearing:

- **It is checked against `tailored_resume_id`, not trusted alone.** If the row
  no longer points where the pin says, something else moved it, and re-tailoring
  is safer than uploading a document the application is no longer attached to.
- **The stored diff is marked, not cleared.** It is still the honest account of
  what tailoring did to the document the owner's version came from, and it
  carries the guard's refusal count — the one number that says whether the model
  kept trying to invent. What it must not do is go on reading as a description
  of the file about to be sent, so it gains `owner_pinned` and the panel says
  the changes describe the earlier document.

`tests/test_review_resume_edit.py` covers both ways of choosing. They are real
gates: with the pin removed, three go red and the log shows
`tailored_resume_published … version=3` landing on top of the owner's pick.

### Editing the résumé on the review screen

The review screen showed the attached document and could not change it. A
tailored bullet that read wrong left two options — reject the application, or
send it anyway — and editing the base on the résumés page did not help, because
a résumé already tailored for this posting is not the base.

`POST /applications/{id}/resume/edit` is the smaller path, and
`packages/tailor/revise.py` is the reason it is not a second implementation:
both edit routes render the PDF, rebuild `raw_lines`, and version rather than
mutate through one function. A second copy of that sequence is how one of them
ends up storing `parsed_json` with no file — invisible until an employer
receives the old PDF.

**Scoped to the application, and that is the whole design.** The edit lands on
`tailored_resume_id`; the profile's base moves only if the owner ticks the box.
On this screen the subject is one employer, and a résumé written for one posting
is a poor starting point for the next — adopting it silently would make every
future application inherit this job's phrasing from a screen that never
mentioned them. `ApplicationResumeEdit.adopt` defaults to `False` for that
reason, and is a separate schema from `ResumeEdit` precisely so the two
defaults can disagree.

Parked only. A `running` application is mid-fill in a browser and a `submitted`
one has already sent its file; editing either changes a screen without changing
anything an employer sees.

The guard is not applied to the owner's own edit, and that is deliberate — the
same reasoning `packages/tailor/edit.py` already records. §2.1 constrains the
*model*, not the owner writing their own history. `raw_lines` is rebuilt so a
fact the owner adds is source text for the next tailoring pass, rather than
something the guard refuses on their behalf.

**Over MCP the author is a model, so there the guard runs.** `edit.py`'s
reasoning turns on who is typing, and a tool call inverts that: an unguarded
résumé write handed to an assistant is exactly the door §2.1 closes — it would
let a model put an employer, a credential or a metric onto a document going to
a real employer under the owner's name, checked by nothing.

The API cannot tell a person from a model, so the caller declares it:
`ApplicationResumeEdit.guard`, off for the dashboard, and sent `true` by
`apps/mcp/server.py` with no parameter that could say otherwise. A settable
guard would be an opt-out from a non-negotiable by argument, which is the same
shape as the `.env` opt-out §7 refuses; `test_the_tool_cannot_turn_the_guard_off`
holds the tool's schema to it.

Only *added* lines are checked. The guard is strict enough that some genuine
source text does not survive a round trip, so re-judging carried-over lines
would refuse edits that changed nothing. The check is document-wide rather than
scoped to one employer's entry — an added line has no entry to be scoped
against yet — so cross-entry borrowing is not caught on this path and callers
should not assume it is. `packages/tailor/revise.py::guard_edit` says so at the
point it matters.

A refusal is not a verdict on the fact. If the owner says it is true, the answer
is for them to type it on `/review`, where the edit is theirs — not for an
assistant to reword it past the check. Both the tool docstring and
`docs/USAGE.md` say that, because it is the one place this design could
otherwise push a model toward laundering a claim.

`GET /applications/{id}/resume` exists for the same reason `revise.py` does:
"which résumé is attached" now has three readers — the review screen, the MCP
tools, and the uploader — and a client working it out for itself is how a screen
ends up describing a document other than the one being sent.

### Nested forms on the review card

Found while adding the editor, and worth recording because it was silent.

`TailoringCompare` renders forms of its own, and the review card wrapped it in
the approve `<form>`. An HTML parser drops a nested `<form>` start tag, so on
the server render those buttons were inert — the feature worked when reached by
client-side navigation and not on a fresh load, which is the hardest kind of
bug to believe a report of.

The approve form no longer wraps the document panels. Its button and note bind
to it by `form` id instead, and `Submitting` takes `pending` explicitly because
`useFormStatus` reads the enclosing form and a detached button has none. Served
HTML for `/review` now has form nesting depth 1.

### Remoteness was allowed to override the region

The owner's search area is one sentence — **California on-site or remote, the
rest of the United States remote only, nothing abroad** — and until now no
single place in the code held it.

`filters.location_matches` was the hard filter that decides whether a `Match`
row is written at all, and it began `if is_remote(posting): return True`. So
the word "remote" anywhere in a location bought a posting past the region
check entirely. Against the twelve crawled postings the one survivor for a US
profile was **`Canada - Remote (ON, AB, BC, or NS Only)`**; `Remote (India
only)` and `Remote - EMEA` pass the same way. A remote job the owner is not
eligible to hold is still a job they cannot hold, so remoteness is the wrong
thing to short-circuit on.

The other half of the filter compared the posting against `profile.location`
as a substring, which §1 rules out directly: a search area is the owner's
input, not a reading of their profile. It also answered the wrong question —
"is this near where I live" rather than "did I ask to see this" — and made
moving house silently rewrite the feed.

`locality.reachable` is now the single statement of the area, and both the
scoring gate (`filters.location_ok`) and the feed (`search.matches`) call it.
`Settings.search_remote_outside_california` supplies the standing preference
and `?remote_outside_california=false` turns it off for one call, which is
what relocating would want. It is a separate setting from `search_us_only`
because it is separately reversible.

Three findings came out of pointing it at real data rather than fixtures:

- **A bare "Remote" was classified as probably-foreign.** `locality_of` had no
  case for a location that names a working mode and no place, so "Remote" fell
  through to `UNPLACED` — the class the corpus says is foreign — and `us_only`
  dropped it. That is the commonest way a *domestic* board writes exactly the
  job the owner is looking for. A mode-only string is now `UNKNOWN`, which is
  what it means: no evidence about where. `UNPLACED` **is now kept too** —
  see the merge note at the end of this section.
- **"distributed systems" meant remote work.** `distributed` was in the remote
  vocabulary and that vocabulary was matched against the *description*, so any
  posting mentioning distributed systems read as offering remote work — and
  under the area rule that converts an on-site role in any state into a
  reachable one. On the twelve crawled postings the old vocabulary called
  **7 of 12 remote and the new one calls 4**; all three that flipped mention
  "distributed", and one of them — `Sr. Manager, Field Engineering`,
  `Northeast - United States` — was kept by the area filter purely on that
  misreading. A location field is a declaration about the job and prose is
  not, so the two now have separate vocabularies: the body needs "distributed
  team", "work from anywhere", or "remote" proper.
- **There were two definitions of remote.** The feed read the description with
  an on-site override; the scoring gate read only title and location. Under
  this rule that disagreement is the difference between a kept job and a
  dropped one, so `locality.reads_as_remote` is now the only definition and
  both call it.

On the twelve crawled postings the area keeps **none of them**, and that is the
right answer rather than a regression: the board is a Palantir sweep with no
Californian and no US-remote roles on it — every posting is abroad or on-site
in another state. Worth stating plainly, because "0 kept" and "the filter is
broken" look identical from the summary line.

Two things this did **not** fix, filed rather than papered over:

- ~~`rescore` leaves a `Match` row behind when a posting newly fails a hard
  filter, so its own "top 1" still prints the Canadian role it just
  excluded.~~ Fixed: `score_and_store` withdraws the stale row and `rescore`
  reports the count, so the live run now reads "0 kept (12 excluded by a hard
  filter, 1 withdrawn)" above "top 0" rather than above the role it excluded.

  Withdrawn **only when the row holds nothing of the owner's**. `decision` is
  their swipe and `tailored_resume_id` is a résumé some provider call already
  paid for; deleting either to tidy up a score would throw away the more
  valuable half of the row to fix a cosmetic one. Those rows stay, and the
  read-time filter goes on hiding them —
  `test_a_decided_match_is_kept_even_when_it_stops_qualifying` holds that line.
- ~~The US city and non-US city lists in `locality.py` are hand-written, so a
  posting in a US city on neither list reads as `UNPLACED` and is dropped.~~
  Measured and closed twice over — the lists were extended, and then
  `UNPLACED` stopped being dropped at all, which removes the failure mode
  rather than narrowing it. The lists are still hand-written and still matter
  for *precision*: an unplaced Californian city is kept, but as a maybe rather
  than as California.

### Merging with the country fix that landed on main

Two sessions fixed the same filter from different ends, and the merge is worth
recording because each caught what the other could not see.

**main had the better data.** It ran against a real crawl of all 119 companies
and found that `location_matches` was a substring test: the owner's profile
reads `san fransico , ca,usa`, and `ca` is inside `canada`, `costa rica` and
`vancouver` and inside none of `united states`. Every Canadian role passed as
Californian while American ones were dropped — the top of the feed was four
Elastic roles in Canada and a finance manager in Costa Rica. It also found
`Spain (Remote)`, `United Kingdom (Remote)` and `Republic of Ireland (Remote)`
sitting in the top three, and two Synthesia roles located simply `Europe`. Its
`locality.py` gained thirty-odd countries plus continents and blocs, and
deliberately left out `georgia`, `panama`, `lebanon` and `jordan` because
those names are American places and rule 1 runs before any state *name* is
read.

This branch had only the twelve-posting Palantir sweep, so none of that was
visible here.

**Three things were reconciled rather than picked:**

- **`UNPLACED` is kept.** main's argument — only an explicit foreign signal
  should exclude, a city no rule recognizes should be ranked down rather than
  hidden — is right, and its own country work is what makes it right: most of
  what used to land in `UNPLACED` now lands in `ELSEWHERE`. It also dissolves
  the 143-missing-cities problem instead of managing it.
- **The region rule stays, because the owner asked for it.** main read *which
  part* of the US as a ranking question, which is correct in general and is
  what `locality.rank` already does. The owner overrode it: on-site outside
  California is a move, not a commute. `remote_outside_california=False`
  restores main's reading exactly, and the test that used to assert it now
  asserts both halves.
- **A bare `United States` is not "outside California".** main's test caught
  this: `UNITED_STATES` covers both `Austin, TX` and a bare `United States`,
  and the region rule was dropping the second. `locality.names_us_region`
  separates them — a location that names no region is no more evidence than
  silence.

### The city lists failed closed, and California was the worst of it

Filed as a footnote to the search area and measured immediately after, because
"a domestic city that is missing fails closed" turned out to describe most of
California. Of 205 Californian cities, **143 did not classify** as Californian
from a bare name — Santa Ana, Stockton, Chula Vista, Palm Springs, Beverly
Hills among them. Under the area rule that is precisely an on-site Californian
job, the one category the owner most wants, dropped in silence.

Three distinct failures, which is why counting them separately mattered:

- **Simply absent** — `Santa Ana` → `UNPLACED` → dropped.
- **Claimed by a substring of another US city** — `Manhattan Beach` matched
  `manhattan` and `La Mesa` matched `mesa`, so both read as `UNITED_STATES`.
  Right country, wrong state, and on-site outside California is dropped.
- **Claimed by a foreign city** — `Dublin` → `ELSEWHERE`.

The first two are now fixed: 135 Californian and 37 other US city names added,
and 177 of 178 unambiguous Californian cities place correctly (Menifee was the
one that got away in the first pass and is in too).

The third is deliberately *not* fixed. `Dublin`, `Ontario`, `Orange`,
`Fairfield`, `Norwalk`, `Westminster`, `Brentwood`, `Carson`, `Davis`,
`Martinez` and a dozen more are real Californian cities whose bare name reads
as somewhere else at least as often. Adding them trades a missed job for a
false positive, and here the false positive is worse: it puts a Dublin,
Ireland role in a feed whose on-site tier is supposed to mean California. Every
one of them classifies correctly the moment a board writes ", CA", which is how
the smaller ones are almost always written. `_AMBIGUOUS_CALIFORNIA_CITIES`
records the decision so the next reader knows it was one.

Two things worth keeping visible:

- **The non-California half matters only because of remote.** An on-site job
  in Bentonville is dropped by the area rule regardless. But `UNPLACED` is
  dropped *even when remote*, so a missing US city name costs exactly the
  remote roles the owner would take. 42 of 88 checked names were missing.
- **A de-duplication pass silently moved `richmond` and `santa rosa` out of
  California.** A name in both a Californian list and the US list is
  unreachable in the second, since California is checked first — so removing
  "the duplicate" removed the live one and both cities became merely American.
  `test_no_city_is_claimed_by_two_lists` now fails on that, which is how it
  was caught.

This does not make the approach right. Hand-written lists are still the reason
a city can be missing at all, and the honest fix is a real place-name dataset
rather than a longer tuple. What changed is the size of the hole and whether
a regression in it is visible.

### Closing the gate gaps §15 had only described

Four of the gaps recorded above were fixable without new data. They are, and
this section says what changed and what deliberately did not.

**Gate 3 now runs the rest of Phase 3.** The gate tested fabrication and the
PDF round trip; the cover letter and the tailoring cache were built, tested,
and never run by it. `make gate-3` now also runs `test_cover_letter`,
`test_apply_cover_letter`, `test_tailor_cache` and `test_apply_uploads_tailored`
— 170 tests rather than the fabrication suite alone. The last of those is in
for the reason the others are: it asserts the tailored file is the one that
reaches the employer, which is the defect that survived a green Gate 3 for as
long as nothing checked it.

**Gate 6's rules read English now, not the fixtures.** The rejection pattern
was written beside the corpus that exercises it, so it matched "with other
candidates" and missed "with another candidate" — the same sentence as
recruiters write it. Measured against fourteen phrasings taken from how
rejections are actually worded, **it caught 2**. It now catches 14, and the
labeled 30 are unchanged at 29 correct, 0 wrong.

Two things made that safe to widen. Rejection is matched *first*, so a loose
pattern there mis-files everything else — every new alternative therefore names
an outcome ("not successful", "has been filled") or somebody else getting it
("forward with another"), never a bare verb; "moving forward with your
application" is an interview and stays one, and a test holds that line. And it
matters most where there is no model: Ollama is not always running, Bayes reads
the realistic rejection as `interview` at a margin of 0.073 and is correctly
refused, so before this the chain resolved to `noise` and the application sat
in the tracker looking live.

**The seniority filter is reachable from a real run.** `filters.seniority_ok`
could always reject a rung mismatch and never did: no production caller set
`target_seniority`, so it defaulted to None and returned True in every crawl,
discover and rescore. It was reachable only from the benchmark — which is
where the number comes from: arming it takes P@10 from 0.900 to 1.000 on the
Gate 5 set, and the posting it removes is a Junior Backend Engineer with an
excellent technology match, exactly what a cosine cannot refuse on its own.

`profiles.target_seniority` is the owner's statement of their rung, and the
fallback lives in `apply_filters` rather than `score_and_store` so every caller
of the hard filters gets it. **NULL means "do not filter on level"**, which is
what every existing row gets and what the migration deliberately does not
backfill: §1 keeps a search filter separate from the profile's description of
the applicant, and inferring a rung from a résumé would narrow the feed on an
inference nobody made. It is a `SeniorityLevel` enum rather than a string
because a typo does not raise — `seniority_ok` passes everything for a target
it cannot place in the ladder, so `"Senior"` would read as "no preference" and
the feed would look unfiltered with nothing to explain it.

**Validation records itself.** `make validate-seeds` printed its verdict and
stopped, so which entries anyone had checked lived in a terminal scrollback.
`--write` (or `make validate-seeds-write`) stamps `checked` and `state` on each
entry and moves a dead board into `retired:` **with the statuses that condemned
it, rather than deleting it** — which is what this file has claimed happened
since the first sweep and never did. A slug that 404s today may be a rename
rather than a departure, and the evidence is what tells those apart later.
`load_seed` reads `companies:` only, so a retired board is not polled. The file
now answers "how many have never been checked" on its own: today, **119 of
119**, because the stamp is new even for the 29 that were swept.

### The labeling loop, and the bias it had to avoid twice

`docs/BACKLOG.md` P1 — "the one that matters most", and the blocker under
every ML claim in this repo. Every relevance label here is a `FIXTURE`: a
posting and a grade written together, beside the code that reads them. That is
why `docs/ML_EVALUATION.md` refuses to name a production ranking candidate,
and why §15 above says Gate 5 does not answer the question it was written to
ask.

`/label` is the screen that changes it. A real crawled posting, a 0–3 grade,
exported as `Provenance.OWNER` by `make export-labels kind=owner`.

**The design decision worth recording is what it is *not*.** The obvious build
is `/swipe` with four buttons instead of two. `packages/matching/feedback.py`
already names two weaknesses in swipe-derived labels, and that build fixes
exactly one of them:

- a swipe is **binary**, so it cannot tell "would apply" from "would drop
  everything for" — which is precisely what NDCG's `2**rel` gain exists to
  reward;
- a swipe is **taken in feed order**, so it is only ever recorded for postings
  the ranker already surfaced. Nothing it buried is ever labeled, and the model
  ends up graded on its own shortlist.

A four-point scale on the same feed fixes the first and leaves the second
untouched — while stamping the result `owner`, the provenance a benchmark
trusts *most*. That is worse than not fixing it at all, because the bias stops
being visible. A `FEEDBACK` label announces its own narrowness; an `OWNER`
label carrying the same narrowness does not.

So `packages/matching/active.py` draws from three streams:

- **uncertain** — scored near the middle of the *observed* range. Read rather
  than assumed: §15 records that the shipped `min_match_score` of 0.75 was
  unreachable when the first real run over 10,922 postings peaked at 0.271, so
  a midpoint hardcoded at 0.5 would call nothing uncertain and this stream
  would silently return empty.
- **unseen** — crawled but never scored for this profile, including postings a
  hard filter dropped. These have no `Match` row at all, so they can never be
  swiped, and before this they could never be labeled either. They are the only
  labels that can measure what the ranker is missing.
- **confident** — its own top picks. If those grade 0, the problem is not the
  threshold and no amount of boundary sampling will show it.

Four things are load-bearing:

- **`PostingLabel` is its own table, not a column on `Match`.** A `Match` row
  exists only for a posting that cleared the hard filters and got scored.
  Keying on (profile, posting) instead is what makes the unseen stream
  storable at all.
- **The stream is recorded per label.** It is the audit trail for the bias:
  a corpus that turns out to be all `uncertain` has the shortlist problem
  back, and after the fact that is otherwise unknowable. `/labels/summary`
  says so outright when no unseen grade exists yet.
- **A scored posting can never be recorded as `unseen`.** Found by a test:
  the first classifier fell through to `unseen` for any score far from the
  midpoint, so the summary would have reported a shortlist-only corpus as
  having escaped the shortlist — the one wrong answer that column exists to
  prevent. `unseen` now means one thing only: the ranker never scored this.
- **A short stream does not backfill from the others.** On a database with
  nothing unscored the unseen quota goes unfilled rather than being handed to
  `uncertain`. Backfilling would quietly return an all-shortlist batch, which
  is this bias arriving through the back door.

**What is not done, and no code can do it.** The corpus is empty. Grading is a
person reading real postings. P1's "done when" is now ≥100 labels *across more
than one stream* — the count alone was never the property that mattered.

### What is still not fixed, and why

- **Gate 1's live half is refused, not pending.** It reaches the form and is
  blocked by a captcha. §2.5 makes captcha-solving a hard scope boundary, so
  making this gate pass *is* the prohibited thing. It should stay red forever;
  what it proves is that the refusal works.
- **Gate 2's live half.** Needs a real posting reached over the network, and
  a real profile. `make gate-2-live URL=...` is the other half.
- **The 90 unvalidated boards.** `--write` exists; the sweep needs network
  egress and has to be run from the owner's machine.

### Getting real data in, for the two gates that need it

Gate 5's labels and Gate 6's emails cannot be written — that is the whole
point of §15. What *was* missing is the path from the owner's actual material
to the number, and both ends of it existed with nothing joining them.

**Gate 5: the judgements were already being collected.** `Provenance.OWNER`
and `FEEDBACK` were defined when `labels.py` was written and nothing ever
produced one, so every label in the repo is a `FIXTURE`. Meanwhile `/swipe`
has been writing `Match.decision` on every yes and no — which *is* a relevance
judgement about a real posting. `make export-labels` reads them back out as a
`LabeledSet` the benchmark can run on.

It exports as `FEEDBACK`, not `OWNER`, and `packages/matching/feedback.py`
says why at length. Two limits are load-bearing. A swipe is **binary**, so it
can only produce relevance 0 or 2 — it cannot express the gap between "would
apply" and "would drop everything for", which is exactly the gap NDCG's
`2**rel` gain exists to reward. And a swipe is **taken in feed order**, so it
is only ever recorded for postings the ranker already surfaced: nothing it
buried is ever labeled, and the model ends up graded on its own shortlist.
Real judgements about real postings, and not interchangeable with graded ones.

**Gate 6: `make import-mail src=<mbox|eml|dir>`.** Reads a Gmail export into a
worksheet — one row per message, the classifier's guess beside an **empty**
label field — and `make score-mail ws=...` reports accuracy over the rows the
owner filled in.

`guess` and `label` are deliberately separate fields, and a row with no label
is excluded from scoring entirely. Collapsing them would let the classifier
grade its own homework: accuracy would be 100% by construction, which is the
self-evaluating referee §45 and the master spec both refuse.
`test_the_worksheet_leaves_every_label_empty` holds that line.

The worksheet stores sender, subject, body and date and nothing else. It is a
second copy of other people's writing about the owner (§14), so it holds the
minimum a classification decision needs.

Neither of these produces a number on its own. They turn "this gate needs data
that does not exist" into two commands and an afternoon.

### Every path read one résumé, and two of three were unreachable

`docs/BACKLOG.md` P2, and the smallest real defect in the register. Every path
read `profile.base_resume_id`: upload a backend résumé, a data one and an ML
one, and the application always starts from whichever the profile happens to
point at.

Silent, which is what makes it worth recording rather than just fixing.
Nothing raises, nothing is logged, and the tailorer does its whole job — the
guard runs, the projects are selected, the diff renders. It rewrites the wrong
document well, and the employer receives a competent ML résumé for a backend
role. The only way to notice is to remember which résumé the profile points
at.

`packages/matching/pick_resume.py` scores each of the owner's base résumés
against the posting and takes the closest. Four things are load-bearing:

- **Tailored rows are excluded.** They live in the same table, and a résumé
  already bent toward another job is the one selection that would be actively
  harmful. `tailored_for_posting_id` is the discriminator — `publish.py` sets
  it on every tailored row including the uncacheable ones where
  `tailored_key` is NULL, `revise.py` carries it across an owner's edit, and
  it is a foreign key, so a tailored résumé always names a posting that
  exists and can never read as an upload by accident.
- **A win inside the noise is not a win.** Two résumés for adjacent roles
  score within thousandths of each other on most postings, and letting that
  decide would change the document an employer receives between two runs of
  the same posting. Below `MIN_MARGIN` the profile's own `base_resume_id`
  wins — the owner's standing choice is the tie-break, not the float.
- **The choice is recorded and then reused.** Approving a parked application
  re-enters the pipeline from the top, so re-deciding would let an upload
  between the two runs swap the document the owner reviewed. Same defect as
  `resume_pinned`, one screen earlier.
- **It takes the posting's text, not a `Posting`.** The apply pipeline hands
  `_tailor` a parsed page rather than a stored row — the parameter is `Any`
  for that reason — so depending on the model would have failed on the one
  caller that matters. Two existing tests caught it.

`/review` names the résumé it started from, its reason, and the runners-up
with their scores. A selection with no alternatives shown is unauditable, and
this decides which document an employer reads first.

The cover letter is written from the same document. A letter drafted off a
different résumé than the one attached would contradict it.

### Three thousand careers URLs, and how many are actually a problem

The owner has a CSV of ~3,000 companies with careers URLs.
`scripts/import_portals.py` already stated why they cannot simply be added:

> A company whose careers page is its own site (`twilio.com/careers`) is
> reported and skipped: we have no extractor for a bespoke page, so adding it
> to the registry would mean a crawl cycle that fetches and parses nothing
> every hour, forever.

So the first question is not "how do we scrape 3,000 sites". It is **how many
of them are bespoke at all** — careers URLs very often already *are* a
Greenhouse, Lever, Ashby or Workable board, and each of those needs no new
code: it is a registry row the existing crawler polls first-hand, through the
rate limiter and robots.

`make import-csv src=companies.csv` answers that. Four things are deliberate:

- **Offline.** No network, so 3,000 rows sort in **0.02s** (measured, and
  asserted at that size). A probe per row would make the answer depend on
  which sites happen to be up, and turn a question worth re-asking while
  cleaning the sheet into an hours-long crawl.
- **The remainder is a file, not a number.** Bespoke rows are written to a CSV
  with every original column preserved — a sheet carrying a sector or a
  headcount keeps it, because whatever reads it next may want the biggest
  companies first. A count tells you the size of the problem; the list is what
  the next tool takes as input.
- **"No URL" and "cannot read this page" are separate buckets.** They need
  different fixes, so collapsing them would hide the cheaper one.
- **It never writes to the registry.** `scripts/import_companies.py` appends
  through `import_portals.append_to_registry`, so there is one door. The test
  checks the AST rather than the source text, because the module docstring
  names that function to say it is somebody else's job and a grep would read
  the explanation as the violation.

A link to one posting still yields the board — `board_root` anchors to the
front page on purpose, and a hand-assembled sheet is full of deep links, so
discarding those would count companies we *can* crawl as bespoke, which is the
one number this exists to produce.

### On Scrapling for the bespoke remainder

Recorded because the library is a reasonable idea whose headline feature §2.5
forbids outright. Its README: *"Its fetchers bypass anti-bot systems like
Cloudflare Turnstile out of the box"*, plus a spider with *"automatic proxy
rotation"* that *"backs off when it starts blocking you"*, and
`StealthyFetcher.fetch(...)  # Fetch website under the radar!`. That is
captcha bypass, bot-detection evasion and proxy rotation — three of §2.5's
clauses, and not incidentally: it is the pitch.

Its *parser* is a different matter and is BSD-3, so §3's cost rule is fine. If
it is ever adopted it must be as a parser only, behind `PoliteFetcher`:
Scrapling's fetchers honour neither robots.txt nor our per-host floor, so
letting it fetch turns §2.6 from a control into a comment.

Two further limits worth keeping visible:

- **Adaptive selectors are wrong inside an ATS form.** Its value proposition
  is "if the element moved, find one that looks similar". §2.2 makes
  work-authorization answers verbatim because they have legal consequences,
  and §2.4 says an unanswerable question parks rather than guesses. A drifted
  selector must fail loudly, not find a plausible neighbour.
- **It buys nothing on the boards we already read.** The crawler fetches
  `boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` — JSON. There
  is no HTML to adapt to.

For a bespoke careers page the higher-yield path is **JSON-LD**: career sites
publish schema.org `JobPosting` for Google Jobs, already structured, with no
selector to drift. ~~That is the next build~~ — built, below.

### Reading a bespoke careers page

`packages/crawler/jsonld.py` is that extractor, and `seeds/bespoke_careers.csv`
— the remainder `make import-csv` writes — is its input.

Structured data rather than selectors because career sites publish
`JobPosting` **so that machines read it**: it is what Google Jobs indexes, and
a site that wants its roles found keeps it accurate. That makes it the one
part of a bespoke page with a stable contract. §15 already records what the
alternative costs — a Greenhouse fixture had native `<select>` while the live
board had moved to react-select, and the suite stayed green while the adapter
misread every dropdown. Multiply that by three thousand sites and it is not a
maintenance burden, it is the whole project.

Four things are load-bearing:

- **A page with no JSON-LD yields nothing, and that is the answer.** Not a
  reason to start guessing at markup. The count of pages that yield nothing is
  the measurement that says whether a second strategy — sitemaps, then
  rendered HTML — is worth building, and `make probe-bespoke` is what produces
  that count.
- **`import_portals.py`'s rule survives having an extractor.** It refused
  bespoke pages because "adding it to the registry would mean a crawl cycle
  that fetches and parses nothing every hour, forever". A page that publishes
  no structured data is still exactly that, so `make probe-bespoke` promotes
  only the pages that answered — and a board yielding zero postings reads
  identically to a board with nothing new, which is the failure §9 Phase 5
  already recorded for the 21 dead Greenhouse slugs.
- **A fetch failure is not a verdict.** Blocked by robots, a 403, a timeout:
  each is its own state and none of them promotes or retires anything. A site
  that is down this afternoon is not a site without structured data, and
  `make validate-seeds` treats a `jsonld` seed the same way — it asks whether
  the page still publishes, because a careers page returns HTML and the JSON
  board check would condemn every live one.
- **A `jsonld` seed's slug is its page URL.** The four board APIs derive their
  URL from a company slug; a bespoke page has no such rule, the address *is*
  the identity. Carrying it in `slug` rather than special-casing the crawler
  keeps `(ats, slug)` the de-duplication key it already is in `discover.py`,
  `import_companies.py` and `import_portals.py`. It is deliberately kept out
  of `EXTRACTORS` for the mirror-image reason: callers that iterate that dict
  build a URL from a slug, which is the one question this extractor cannot
  answer.

Nothing here fetches. `PoliteFetcher` gets the bytes, because it is what
enforces robots.txt and the per-host floor (§2.6), and an extractor that
fetched for itself would route around both. The floor costs nothing on this
sweep — thousands of *distinct* hosts, one request each, so no two are ever
serialized behind one counter.

**Not built, and worth stating rather than implying.** There is no sitemap
path: a site that publishes `JobPosting` only on individual posting pages,
with none on the index, still reads as empty. And the sweep needs network
egress from the owner's machine, like `make validate-seeds` — so how many of
the ~3,000 bespoke pages actually publish is, today, an unmeasured number.

### What two outside specs were worth

The owner supplied a job-tracker prompt (MERN/TypeScript LinkedIn scraper) and
an AI-résumé-analyzer prompt (MERN + Gemini). Both are build-from-scratch
specs for other stacks, so nearly all of it is either irrelevant here or
already done better. Three things were not, and this records which — so the
next person does not re-read forty pages to find out.

**An assessment is its own kind of message.** The tracker's pipeline carried
`ONLINE_ASSIGNMENT` as a stage of its own; ours had no equivalent, and the
three commonest phrasings landed badly:

    "Complete your online assessment ... within 5 days"  -> info_request
    "Coding challenge ... Codility link. 72 hours."      -> abstained
    "Take-home exercise ... return within a week"        -> abstained

`info_request` reads as paperwork and an abstention resolves to `noise`, so
either way the window expires while the tracker looks calm. That is a worse
failure than a missed rejection — a rejection is already over, an assessment
is an opportunity with a deadline. `Classification.ASSESSMENT` and
`Outcome.ASSESSMENT` now exist, ranked above `info_requested` and below
`interview`, because an assessment is a real advance that almost always
precedes an interview rather than replacing one.

The rule sits *before* `INTERVIEW` — an assessment invite borrows the same
"next step" vocabulary — and every alternative names the artefact or a
platform rather than a bare "assessment", so "we will assess your
application" is not a coding test. The labeled 30 are unchanged at 29 correct,
0 wrong.

**Three Gemini facts worth having before a key lands**, from the analyzer
spec's list of failures that cost someone a build. They are in
`.env.example`: keys beginning `AQ.` are as valid as `AIzaSy...` so nothing
should format-check them; setting `GOOGLE_API_KEY` alongside `GEMINI_API_KEY`
makes the SDK silently prefer the former; and `GOOGLE_GENAI_USE_VERTEXAI=true`
switches to Vertex, which wants application-default credentials instead.

**A landmine in `GeminiProvider.complete_json`.** The spec's hardest-won
lesson is that Gemini treats every schema property as optional unless named
in `required`, and returns a partial object with no error otherwise. Pydantic's
`model_json_schema()` already emits `required` at every level, so that half is
covered. What it *also* emits, for a nested model, is `$defs`/`$ref` — which
`responseSchema` has not historically resolved. No caller passes a nested
schema today (`complete_json` has no production caller at all), so this is
recorded at the call site rather than fixed: the first nested schema should
check the response before trusting it.

Everything else was skipped deliberately. The tracker's space-padded substring
matching solves a problem `locality.py` already solves with word boundaries;
its match formula is a weighted keyword count where we have embeddings plus a
rubric; and both specs assume a greenfield MERN app.


### What the first real comparison showed about the tailorer

The compare panel's first live run — llama3.1 against openrouter, on a real
Cloudflare posting, both sides answering with zero provider failures — reported
ollama 11 rewrites against 25 guard refusals, and openrouter 12 against 1. Read
straight, openrouter won decisively. The content said otherwise, and three
defects behind that reading are now fixed.

**A skills line is a list however long it runs.** `classify` consulted
`_is_list` only *after* the length fallback, so `Languages  Python, TypeScript,
Rust, C, C++, Java, ...` was nineteen words and therefore "prose". It went to
the model, which returned `Using tools, I have experience with Python and
GitHub Actions.` — a list rewritten as a sentence, naming fewer things than it
started with. Two of them also carry a category label, which hid them from the
plain comma test as well: splitting `Data, DevOps & Tools  Qdrant (Vector DB),
Docker, ...` on commas puts the label and the first entry in one long fragment.
`_is_labelled_list` splits at the column gap first.

Moving the list test ahead of the length test is only safe because the verb
test already ran, and because `_is_list` now requires no terminal full stop —
otherwise a real bullet built from short clauses would be filed as a stack and
silently skipped, which CLAUDE.md already records as the harder failure to
notice. On the owner's résumé this took the lines sent to the model from 37 to
16, and every one of the 16 is prose.

**A rewrite nobody can see was being counted as a rewrite.** `changed` was
`candidate.strip() != bullet.strip()`, so a colon added to `Core CS  Data
Structures` and two spaces removed from `Cloud Data Warehousing & BI Analytics
[GitHub]` both counted. Three of openrouter's reported 12 were of that kind;
nine were real. `is_substantive` compares the lines with punctuation dropped and
spacing collapsed, and a cosmetic answer now keeps the source line so the
document and the count agree. Case is deliberately *not* folded — a model
lowercasing a technology name has changed something worth showing.

**Deleting a true thing is not fabrication, and nothing was checking for it.**
Every test in `guard.py` reads the output and asks whether it says something the
source does not; none reads what is missing. Both models dropped
`(Pillow/Tkinter)` from the same bullet and neither was refused, so the résumé
came out of tailoring with fewer of the keywords an ATS scans for.
`packages/tailor/technologies.py` reads the technologies the résumé lists for
itself — the skills section and the stack line under each project — and `vet`
refuses a rewrite that drops one.

Scoped to *listed* technologies rather than to names, because
`extract_entities` reads `Filtered`, `Provisioned` and `Designing` as proper
nouns and rejecting every dropped one would refuse the re-emphasis §2.1 permits.
Presence is decided through the alias table, so expanding `CI` to `continuous
integration` keeps the term — that is a rename, not a deletion. A flat corpus
has no inventory and disables the check rather than guessing one.

**What none of this fixes.** Both models still write worse bullets than the
source in places — openrouter inserted `using forecasting and clustering tools`
into a Tableau line and turned `vector retrieval pipeline` into `vector
retrieval data pipeline`. The refusal count measures how hard a model pushed
against the guard, not whether the résumé got better, and tuning against it is
the trap `docs/REFERENCE.md` §3.6 names. These three changes make the counts
mean what the screen says they mean; they do not make either model good.
