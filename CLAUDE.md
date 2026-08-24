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

Implementations: `OllamaProvider`, `GeminiProvider`, `AnthropicProvider`, `StubProvider`.
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

The registry started at 50 and is now 29. `make validate-seeds` found that 21
of the original entries returned 404 from both the board API and the rendered
page — those companies have left Greenhouse. They are listed at the bottom of
`seeds/companies.yaml` with the evidence rather than deleted, because a 404
board is worse than an absent one: it yields zero postings, which reads
identically to "nothing new since the last poll".

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

**Local-only, not configurable.** Chat context carries application URLs,
profile fields, and recruiter correspondence. §2.8 permits exactly one
third-party upload — the tailoring call — and a chat window is not it. So
`apps/api/routers/chat.py` asks for Ollama *by name* rather than reading
`LLM_PROVIDER`, and when Ollama is down it errors instead of falling back to a
cloud provider that happens to be configured. Setting `LLM_PROVIDER=gemini`
changes tailoring and leaves the assistant local.

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

## 15. What the gates do and do not prove

Every gate in §9 passes. Two of them pass against fixtures rather than the real
material their wording asks for, and that distinction is worth keeping visible
where the gates are defined rather than only in a test docstring.

- **Gate 5** asks for "a hand-labeled set of 20 postings — the ones you'd
  actually apply to". `tests/test_matching.py` has 20, written the way real
  postings read. They are not postings the owner labeled.
- **Gate 6** asks for "30 hand-labeled **real** recruiter emails".
  `tests/test_inbox.py` has 30 written to match. They are not real correspondence.

Both suites are worth having — they catch regressions. Neither answers the
question its gate was written to ask, which is whether the scoring and the
classifier work on *this owner's* material. Swap the fixtures for real data
before trusting either number.

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

### Phase 3 scope that is not built

§9 Phase 3 lists two things Gate 3 does not cover:

- **Cover letter.** A module writes one — `packages/tailor/cover.py`, which
  vets every letter against the fabrication guard, refuses one that raises a
  §2.2 topic, and never falls back to an unvetted draft. Nothing calls it. The
  apply pipeline never asks for a letter, `Application.cover_letter_ref` is
  never written, and so no application has carried one. The tests exercise the
  module, not the feature. `docs/PARITY.md` tracks the remaining gap.
- **Per-company caching of tailored versions.** Every apply re-tailors from
  scratch. Harmless today because tailoring is cheap and local; it becomes a
  cost the moment a remote provider is used for it.

Gate 3 passes without them because it tests fabrication and the PDF round trip,
which is the part with consequences. Recorded here so the gap is not mistaken
for completion.
