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

Postgres runs on **5433** on the owner's machine, not the 5432 in the row above:
another project holds 5432. The remap lives in an uncommitted
`docker-compose.override.yml`, and `.env` points at 5433 to match.
Both files publish on **127.0.0.1** only: the role is a superuser with the
compose default password, and a bare `5432:5432` put it on the network
(`tests/test_database_binds_loopback.py`).

`next dev` binds **127.0.0.1:3001**, not the localhost:3000 the row implies.
Port 3001 because 3000 is taken. `next start` binds 127.0.0.1 too.

The dashboard binds loopback by default and has to keep doing so. `/api/*` is
proxied to the API from 127.0.0.1, so the API's own loopback guard never sees
a network client; binding the dashboard to the LAN put the whole API on it.
`JOBRUNNER_WEB_HOST` is the typed-out opt-in, and
`tests/test_dashboard_binds_loopback.py` holds the default. The measurements,
and the notes on the frontend row and the two effect libraries, are in
`apps/web/CLAUDE.md`.

---

## 4. Repo layout

The directory tree that was here is what `ls` shows, so it was removed on
2026-10-11. What `ls` does not show is where this file's notes now live. Each
of these loads only when work touches the directory or the files it names:

| File | Holds |
|---|---|
| `apps/api/CLAUDE.md` | the assistant's rules and chat memory (§14), streams (§17), the dev server's reload (§22), the wiring check (§26), what a reply says for itself (§27) |
| `apps/mcp/CLAUDE.md` | the MCP tools (§9, Phase 4) |
| `apps/web/CLAUDE.md` | the loopback bind and the effect libraries (§3); the feed's wording, the tracker, the design pass (§15) |
| `apps/worker/CLAUDE.md` | which résumé is uploaded, and the owner's pinned choice (§15) |
| `apps/dab/CLAUDE.md` | Data API Builder (§23) |
| `packages/llm/CLAUDE.md` | providers, routing, OpenRouter, the fallback (§7); the local model and Ollama's hosted models (§14) |
| `packages/tailor/CLAUDE.md` | comparing two models (§7); tailoring, the guard, the cover letter, the cache, editing at review (§15) |
| `packages/ats/CLAUDE.md` | the adapter contract (§8); reaching and filling a form (§15) |
| `packages/crawler/CLAUDE.md` | the registry (§9, Phase 5); bespoke pages and the shared rate limiter (§15); duplicate boards (§25) |
| `packages/matching/CLAUDE.md` | the search area, eligibility, experience, labelling, résumé selection (§15); the feed's authorization filters (§18) |
| `packages/inbox/CLAUDE.md` | the reply alias and duplicate mail (§15) |
| `packages/core/CLAUDE.md` | the doorbell (§15); the queue's claim order and its producers (§24); races (§27) |
| `tests/CLAUDE.md` | what each gate proves, and the test database (§15) |
| `docs/CLAUDE.md` | outside repositories and specs (§15); hybrid retrieval (§19) |
| `.github/CLAUDE.md` | CI, the Dockerfile, pinned dependencies (§13) |
| `.claude/rules/assistant-search.md` | the assistant's search (§14, §20, §21, §22, §27); loads with `retrieve.py`, `rerank.py`, `roles.py` and the chat routes |

"§N" in code, in tests and in those files is still a section of this file:
every heading is kept here, with a line saying where its text went.

---

## 5. Data model

Removed on 2026-10-11. `packages/core/models.py` is the authoritative
definition, and the summary that was here only repeated it.

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

Every provider sits behind `LLMProvider` in `packages/llm/provider.py`, and
`LLM_PROVIDER` selects one. What has to hold everywhere:

- No test may hit a network LLM. Tests use `StubProvider`, and nothing in
  production falls back to it.
- The assistant and inbound-email classification are not in
  `CHOOSABLE_TASKS`: no environment variable moves them off the local model
  (§2.8).
- `openrouter` and `ollama_cloud` answer only when named, and are absent from
  `router.QUALITY_ORDER`.

Task routing, temperatures, OpenRouter, the local fallback and what the review
screen records are in `packages/llm/CLAUDE.md`. Comparing two models on one
posting is in `packages/tailor/CLAUDE.md`.

---

## 8. ATS adapter contract

`packages/ats/base.py`. Every ATS lives behind this and nothing ATS-specific leaks out.

Field kinds, what a fill report records, the adapter build order and what
running the adapters against live boards found are in `packages/ats/CLAUDE.md`.

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

What the MCP tools do and what must not be undone in them (the tools that
make the client ask, what the owner types, ids in paths, the tracker, status
and grading tools) is in `apps/mcp/CLAUDE.md`.

### Phase 5 — Discovery

Build: company registry (hand-picked, seeded from a YAML file). Crawler with
content-hash change detection and per-host rate limiting. Posting extraction per ATS.
Embedding of postings and profile. Hard filters (location, seniority, work authorization,
sponsorship). Cosine scoring. Match feed in the dashboard.

The registry's history, and how to validate it before trusting a quiet crawl
(`make validate-seeds`), is in `packages/crawler/CLAUDE.md`.

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

Removed on 2026-10-11: these were the steps for creating the repository, which
exists. The rule they ended on stands. Work one phase at a time, and do not
start a phase until the gate before it passes (§9).

---

## 13. CI

`.github/workflows/ci.yml` runs the gates on a machine with nothing installed.
Three things to know before changing it, the `Makefile` gates, the
`Dockerfile` or the dependency set:

- A new `gate-N` has to be named in `ci.yml` too. CI calls `make gate-N-only`
  and never repeats a file list; `tests/test_ci_runs_every_gate.py` holds both.
- The `image` job does not run on pull requests. A pull request that touches
  the `Dockerfile` or the dependency set is checked by hand with
  `workflow_dispatch` before merging.
- Everything installs from `constraints.txt`, and `make lock` refreshes it.

The reasons are in `.github/CLAUDE.md`.

---

## 14. The assistant

`/chat` in the dashboard, and a dock on the review screen. A chat surface over
the owner's own data, answered by a model on this machine.

What has to hold, in one place:

- **Local by default.** A remote provider is named per question in the UI
  (`ChatRequest.provider`). `/chat` ignores `LLM_PROVIDER`, and the assistant
  is not in `CHOOSABLE_TASKS`.
- **No fallback in either direction**, and a hosted model is never given the
  local label.
- **Recruiter mail is withheld from a remote provider** unless `share_mail` is
  set for that question.
- **§2.2 is refused in code before any model is reached**: work
  authorization, sponsorship, employment history, salary.
- **"Run crawler" is a command matched in code**, never inferred from a
  question.

The rules in full, chat memory and the audit trail are in
`apps/api/CLAUDE.md`. The local model, its measurements and Ollama's hosted
models are in `packages/llm/CLAUDE.md`. Retrieval is in
`.claude/rules/assistant-search.md`.

## 15. What the gates do and do not prove

Every gate in §9 passes, and two of them pass against fixtures rather than the
real material their wording asks for: Gate 5's labelled postings are not ones
the owner graded, and Gate 6 has never seen a real recruiter email. Gate 1's
live half stops at a captcha, which §2.5 makes the correct outcome. What each
gate does and does not prove is in `tests/CLAUDE.md`.

The rest of this section was a record, one incident at a time, of defects
that passed every gate. Each now sits beside the code it is about:

| Subject | File |
|---|---|
| tailoring, the guard, the cover letter, the cache, editing a résumé at review | `packages/tailor/CLAUDE.md` |
| the search area, eligibility, experience, labelling, which résumé is picked | `packages/matching/CLAUDE.md` |
| the reply alias, duplicate mail, a resent OTP | `packages/inbox/CLAUDE.md` |
| bespoke careers pages, the shared rate limiter | `packages/crawler/CLAUDE.md` |
| reaching and filling a form, what live runs cannot prove | `packages/ats/CLAUDE.md` |
| which résumé is uploaded, the owner's pinned choice | `apps/worker/CLAUDE.md` |
| the doorbell (`notify`) | `packages/core/CLAUDE.md` |
| the feed's wording, the tracker, the design pass, the LAN bind | `apps/web/CLAUDE.md` |
| the test database, real data for Gates 5 and 6 | `tests/CLAUDE.md` |
| what outside repositories and specs were worth | `docs/CLAUDE.md` |

---

## 16. Additions of 2026-09-14, and what must not be undone

Built against `docs/PROJECT_AUDIT_2026-09-14.md`. Each line is a property a
later change could quietly break.

- **Registry repair.** The 119 company rows predated `Company.slug`, so the
  source-status migration backfilled all of them to `no_website` and the
  dashboard asked for 119 URLs while 11,851 postings were open.
  `sync_registry` now also reads `retired:` and marks those rows `failed`,
  keeping earlier evidence under `previous`; `registry_health` diagnoses
  drift read-only. Live preview on 2026-09-14: 74 created, 181 verified,
  12 retired, Temporal and Runway moved boards. **Not applied** to the live
  database — `/setup` offers it.
- **Setup page never shows a secret.** `tests/test_setup_status.py` runs the
  real vault and inbox checks with sentinel values and asserts neither appears.
- **Unknown is never satisfied.** No pay, a figure with no period, a skill
  outside a requirements heading, or no education: each fails its filter
  unless its `include_unknown_*` switch is set. Extractor v2 exists because
  sampling the live corpus found benefit budgets read as pay and `€95.000`
  read as 95; 6,465 of 14,892 postings state pay after the fix.
- **Search preferences are not profile answers.** `search_preferences` is its
  own table and `FILTER_KEYS` refuses anything the feed does not read.
- **Merging is conservative.** Never across companies, never within one
  source, never with differing requisition ids, never after an owner split.
  Grouping adds a link; no posting, match or application row changes.
- **Base and personalized scores stay separate.** `Match.score` is never
  rewritten; `personalized_score` is computed on read and every adjustment is
  explained. `/ranking/evaluation` reports no learned model and refuses
  promotion below 100 owner grades from two streams.
- **Nothing contacts an employer.** Tasks are local, reminders use the owner's
  own notification backends, and `.ics` files carry no contact details.
- **Backups never contain the vault key**, exclude browser profiles by
  default, and verification refuses the live database and any directory
  overlapping `storage/`.
- **Only the last month of postings is kept** (`POSTING_MAX_AGE_DAYS`, 30).
  The crawler does not store an old posting *new* to the database; one
  already held is still stamped as seen, because `_close_missing` closes
  whatever a crawl did not stamp, and a still-listed posting would be closed.
  `make prune-postings` deletes held postings that are closed or old, never
  one the owner applied to, swiped on, tailored a résumé for, or graded:
  `applications` and `resumes` point at postings `ON DELETE SET NULL`, and a
  tailored résumé that loses its posting reads as a base résumé. An undated
  posting is never deleted for its age. First run, 2026-09-30, after a
  verified backup: 19,807 deleted, 7 kept for the owner, the postings table
  226 MB to 39 MB, and chat search about 3x faster (940 to 329 ms).

  **The prune now runs itself** (`POSTING_AUTO_PRUNE`, on), at the start of
  every matching pass and by the same rules. The limit alone only stopped new
  old postings arriving: the day after that first run, 222 held postings and
  3,132 of their chunk vectors had aged out and were still searched. A
  posting's vectors go with it — `description_embedding` is on the row and
  `posting_chunks` cascades. The pass does not `VACUUM FULL`, which locks the
  tables search reads; the command still does, and now for every table a
  posting delete cascades into, read from the models. Its hand-written list
  had missed `posting_chunks`, the largest of them.

---

## 17. Live status updates, and the process boundary they had to cross

`GET /events/applications` streams status changes by tailing
`application_events`, the log `transition()` writes, because the worker is a
separate process and an in-process publisher would carry only the owner's own
clicks. `POST /chat/stream` streams the assistant's answer. What must not be
undone in either is in `apps/api/CLAUDE.md`.

---

## 18. The feed could not be asked the one question that decides eligibility

`sponsorship` and `exclude_citizenship_restricted` are feed filters over what
`eligibility.py` reads from a posting, and an unknown is never a pass. The
details, and the feed over MCP (`my_matches`), are in
`packages/matching/CLAUDE.md`.

---

## 19. Hybrid retrieval, and a number that could not be read

`packages/matching/hybrid.py` fuses the cosine with BM25 as benchmark variants
only, and `Match.score` is untouched. The runs and what they may claim are in
`docs/CLAUDE.md` and `docs/ML_EVALUATION.md`.

---

## 20. What asking the assistant real questions found

Six defects found by asking the assistant real questions on 2026-10-03, the
retrieval modes measured against each other, and how the assistant reads the
owner's résumés as skill names only: `.claude/rules/assistant-search.md`.

---

## 21. A second model re-orders the assistant's search

A second model may re-order what the assistant's search found. It is off
unless `CHAT_RERANK_MODEL` names one. What was measured, and the
cross-encoder, are in `.claude/rules/assistant-search.md`.

---

## 22. A question that names a kind of job

How the assistant answers a question that names a kind of job (the role
table, kinds of title, title words, connecting words) is in
`.claude/rules/assistant-search.md`. Why `make api` passes
`--timeout-graceful-shutdown 3` is in `apps/api/CLAUDE.md`.

---

## 23. A second, generated API beside the written one

`make dab` serves three sources read-only on 127.0.0.1:5050 through Data API
Builder. It is a second door onto the database and does not inherit the API's
guards. What holds it (`tests/test_data_api_builder.py`) and what running it
showed are in `apps/dab/CLAUDE.md`.

---

## 24. Discovery waits for everything else

`claim_task` takes discovery work only when nothing else is runnable, and a
producer that looks before it enqueues takes a lock first. Both are in
`packages/core/CLAUDE.md`.

---

## 25. One board is polled under one company row

Discovery asks who holds a board before verifying a company, and
`uq_companies_verified_board` refuses the rest. The repair and what was left
alone are in `packages/crawler/CLAUDE.md`.

---

## 26. What checking the assistant's wiring found

Four defects found on 2026-10-06 by running every path the assistant has
(citations in round brackets, "skill" as a search word, a place named in a
question, a remote role abroad): `apps/api/CLAUDE.md`.

---

## 27. What tracing the assistant's paths found

On 2026-10-10 the assistant's retrieval, its routing, the MCP tools and the
queue's producers were each traced through the code and reproduced before
anything was changed. `docs/ML_EVALUATION.md` has the report: the diagram,
the issue table, every number and what is still wrong. This section is only
what a later change could quietly undo.

**The dashboard assistant is not an agent.** One completion over text that
application code read first. The model has no tools and runs no query. The
tool loop is the MCP client's, and its tools go through the HTTP API on
purpose (`apps/mcp/client.py`).

What a later change could quietly undo, by where it lives: the races (the
approval gate's row lock, producers that look before they enqueue, a grade's
offer) in `packages/core/CLAUDE.md`; routing and the semantic experiment in
`.claude/rules/assistant-search.md`; what the model is handed and what the
reply says for itself in `apps/api/CLAUDE.md`.

Fixed: a retried `apply` task sending a form twice (#16, covered by `tests/test_submission_ledger.py`).
Not fixed, and each is in the report: one-word company names read from ordinary words
("Front Desk"); paraphrased questions, where bge-small's chunk search is the limit;
and the role table reading a title only up to its first comma.
