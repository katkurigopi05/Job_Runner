# CLAUDE.md — Jobrunner

Project context for Claude Code. Read this before any task in this repo.

---

## 1. What we are building

**Jobrunner** — a local, single-user job-application agent. It watches a curated list of
company career pages, scores new postings against the owner's profile, rewrites the résumé
per posting, fills the real ATS form in a headless browser, and holds for approval before
submitting. Recruiter replies are ingested and routed back to the application record.

Reference teardown of the commercial product this is modeled on: `docs/TSENTA_ARCHITECTURE.md`.

**Runs entirely on localhost. Zero recurring cost. One user: the repo owner.**

---

## 2. Non-negotiable rules

These are correctness requirements, not preferences. Violating any of them is a bug.

1. **Résumé tailoring never invents facts.** Rewriting may rephrase, reorder, re-emphasize,
   and inject keywords *that are already supported by the source résumé*. It may not add a
   skill, employer, date, credential, or metric that is not in the source. There is a test
   suite for this (`tests/test_no_fabrication.py`) and it is a merge gate.
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
| Frontend | Next.js 15 + Tailwind + shadcn/ui | `next dev`, localhost only |
| Mail | IMAP poll, Gmail `+alias` addressing | `owner+app{id}@gmail.com` |
| Migrations | Alembic | |
| Tests | pytest + pytest-asyncio | |
| Lint/format | ruff + ruff format | |
| Types | mypy strict on `packages/core` and `packages/ats` | |

No paid service is permitted in `requirements.txt` or `package.json` without asking the owner first.

---

## 4. Repo layout

```
jobrunner/
├── CLAUDE.md
├── docker-compose.yml            postgres only
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
├── packages/
│   ├── core/                     models, schemas, state machine, vault, storage, queue
│   ├── ats/                      adapters — base.py + one file per ATS
│   ├── crawler/                  fetchers, change detection, posting extraction
│   ├── tailor/                   résumé rewrite, diff, PDF render, fabrication guard
│   ├── matching/                 embeddings, hard filters, scoring
│   ├── inbox/                    IMAP ingest, classification, routing
│   └── llm/                      provider abstraction
├── migrations/                   alembic
├── storage/                      gitignored — resumes, PDFs, screenshots, browser profiles
├── tests/
└── docs/
    └── TSENTA_ARCHITECTURE.md
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
        content_hash, first_seen_at, closed_at)

Match(id, profile_id, posting_id, score, reasons_json, created_at)

Application(id, candidate_id, profile_id, posting_id, url, ats,
            status, failure_reason, review_json,
            tailored_resume_id, cover_letter_ref, receipt_json,
            created_at, updated_at)
        UNIQUE(candidate_id, url)

ApplicationEvent(id, application_id, type, payload_json, at)   -- append-only

InboundMessage(id, candidate_id, application_id, from_addr, subject,
               body, classification, at)

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

| Task | Default provider | Why |
|---|---|---|
| classify inbound email | Ollama | cheap, local, easy |
| map form field to profile key | Ollama | structured, low creativity |
| tailor résumé | best available | quality matters most here |
| write cover letter | best available | |
| answer open-ended question | best available | goes on a real application |

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

Build: company registry (~50 hand-picked, seeded from a YAML file). Crawler with
content-hash change detection and per-host rate limiting. Posting extraction per ATS.
Embedding of postings and profile. Hard filters (location, seniority, work authorization,
sponsorship). Cosine scoring. Match feed in the dashboard.

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
- Cloud deployment, CI beyond local `make gate-N`
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
