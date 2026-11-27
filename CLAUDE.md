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

   **Amended 2026-10-25, owner approved:** a repository the owner links to a résumé
   Projects entry, on the dashboard only, is evidence for that entry's bullets: its primary
   language, topics, and the tools its own dependency files declare, named through the fixed
   table in `packages/github/manifests.py`. Never its description's prose, never another
   entry, never an employer, never a fork. Details in `packages/tailor/CLAUDE.md`.
2. **Work-authorization and employment-history answers are copied verbatim from the profile.**
   Never LLM-generated. These have legal consequences for the applicant.
3. **Nothing submits without explicit approval by default.** `AUTO_SUBMIT=false` is the
   shipped default. Auto-submit is opt-in per profile and requires a match-score threshold.
4. **Unanswerable question parks the application.** Status becomes `needs_review` and the
   exact question text is surfaced. Never guess, never leave blank, never fabricate.
5. **No captcha-solving services, no bot-detection evasion, no residential proxy rotation.**
   When a site blocks automation, the application fails as `manual_completion_required` and
   the owner finishes it by hand. This is a hard scope boundary.
6. **Crawler respects robots.txt and rate limits.** Between 5s and 60s between requests to
   the same host, 5s by default (`CRAWLER_MIN_DELAY_S`). Never below 5s. A site's own
   `Crawl-delay` and a `429`/`Retry-After` may ask for more than 60s, and win.

   **Amended 2026-11-02, owner approved:** this read "Minimum 60s, configurable up, never
   down" until a description backfill read job pages at 5 s a host with no `429` and
   refusals no higher than at 60 s. The owner set the range to 5–60 s.
   `ratelimit.MIN_DELAY_SECONDS` and `MAX_CONFIGURED_DELAY_SECONDS` hold the two numbers,
   the setting is refused outside them, and the migration `c4e6a8b0d2f4` lowered the hosts
   the crawler had stored at 60 s, since a stored delay only ratchets up.

   **Amended:** a host that is *known* to be a multi-tenant ATS API — one endpoint
   serving thousands of companies' boards, listed explicitly in
   `ratelimit.SHARED_API_HOSTS` — has its own floor of 2s instead. The original rule
   pictures a company's own careers page, where 60s is courtesy. Applied to
   `boards-api.greenhouse.io` it protects nobody: it serializes every company in the
   registry behind one counter, capping the crawler at 60 boards an hour regardless of
   how many are listed. What makes this a narrowing rather than a loophole: the host
   list is explicit so nothing is promoted by resembling it, the 2s floor is refused
   below exactly like the 5s one, a site's own `Crawl-delay` still raises its host's
   delay, and a `429`/`Retry-After` backs that host off for as long as it asks. Polling
   faster is only defensible while also listening.

   **Amended 2026-11-02, owner approved:** `make backfill-descriptions pace=5,10,15,20`
   may read job pages (never lists) of Workday, Rippling and Breezy boards at 5 s a host,
   for that command only. It steps to 10, 15 and 20 s when 30% of the last 60 pages are
   refused, never steps back, and stops the run if refused at 20 s. A `Crawl-delay` and
   a `429` still win. Since the range change above, the crawler's limiters refuse
   anything under 5 s too, and without `pace=` the backfill uses them. `packages/crawler/backfill.py`, `Pace`.
7. **Secrets never touch the database in plaintext and never appear in logs.** ATS account
   passwords go through `packages/core/vault.py` (Fernet, key from OS keychain or `.env`
   outside the repo). `.env` is gitignored and stays gitignored.
8. **Résumés are PII.** Local storage only. No third-party upload except the LLM call needed
   for tailoring, and that call is logged so the owner can audit what left the machine.

---

## 3. Stack (all free)

Python 3.12 services and workers, a Next.js dashboard, Postgres with pgvector.
Versions and tools are in `pyproject.toml`, `apps/web/package.json` and the
`Makefile`. What those files do not say: the queue is Postgres
`FOR UPDATE SKIP LOCKED` (no Redis, one less service); LLMs sit behind the
provider abstraction (§7); embeddings run locally (bge-small, 384-dim);
storage is `./storage/` behind an S3-shaped interface; dashboard components
are hand-rolled, not shadcn; recruiter replies arrive through Gmail `+alias`
addressing (`owner+app{id}@gmail.com`).
One optional Rust extension reads skill vocabularies, `rust/pattern-search-rs`
(`make rust`); without it `re` gives the same answers, 6x slower.

No paid service is permitted in `requirements.txt` or `package.json` without asking the owner first.

Postgres runs on **5433** on the owner's machine, not the compose default 5432:
another project holds 5432. The remap lives in an uncommitted
`docker-compose.override.yml`, and `.env` points at 5433 to match.
Both files publish on **127.0.0.1** only: the role is a superuser with the
compose default password, and a bare `5432:5432` put it on the network
(`tests/test_database_binds_loopback.py`).

`next dev` binds **127.0.0.1:3001**, not the default localhost:3000.
Port 3001 because 3000 is taken. `next start` binds 127.0.0.1 too.

The dashboard binds loopback by default and has to keep doing so. `/api/*` is
proxied to the API from 127.0.0.1, so the API's own loopback guard never sees
a network client; binding the dashboard to the LAN put the whole API on it.
`JOBRUNNER_WEB_HOST` is the typed-out opt-in, and
`tests/test_dashboard_binds_loopback.py` holds the default. The measurements,
and the notes on the frontend stack and the two effect libraries, are in
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
| `packages/crawler/CLAUDE.md` | the registry (§9, Phase 5); bespoke pages and the shared rate limiter (§15); duplicate boards (§25); noted companies and the deep dive (§28); registry repair and posting retention (§16) |
| `packages/matching/CLAUDE.md` | the search area, eligibility, experience, labelling, résumé selection (§15); unknown values, search preferences, merging, base and personalized scores (§16); the feed's authorization filters (§18) |
| `packages/inbox/CLAUDE.md` | the reply alias and duplicate mail (§15) |
| `packages/core/CLAUDE.md` | the doorbell (§15); the queue's claim order and its producers (§24); races (§27); retries, cancelling, time limits, finish times and pruning (§29) |
| `tests/CLAUDE.md` | what each gate proves, and the test database (§15) |
| `rust/CLAUDE.md` | the Rust skill matcher: what holds it to `re`, changing it, the macOS strip (§3) |
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

Transitions go through `packages/core/state.py::transition()` only. It validates the
edge, writes an `ApplicationEvent`, and is the single place status changes. Direct
assignment to `Application.status` anywhere else is a bug. The edges and the
`failure_reason` values are defined there.

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

The phases (0 skeleton, 1 first ATS, 2 profile and résumé, 3 tailoring,
4 MCP server, 5 discovery, 6 tracker), what each built and the wording of
each gate moved to `docs/BUILD_PHASES.md` on 2026-10-26. What each gate
does and does not prove is in `tests/CLAUDE.md`.

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
- Applying through Workday forms. Reading Workday postings and verifying
  boards in discovery are in scope (owner approved 2026-10-20 after the
  Phase 6 gate). Workday application URLs remain unsupported:
  `manual_completion_required` / `unsupported_site`.
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
- **Chunks are 500 characters overlapping by 25%: 125 characters** (owner,
  2026-11-06). DP-800's `AI_GENERATE_CHUNKS(CHUNK_SIZE = 500, OVERLAP = 25)`
  means a percent, which the chunker had read as 25 characters; measured with
  Qwen3-Embedding-4B, 25% beat 5% (MRR +0.028) and 10-30% otherwise did not.
  `chunks.OVERLAP_PERCENT`. A chunk records the overlap it was cut at, so a
  change re-cuts every posting, and its Qwen vectors are made again.

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
that passed every gate. Each now sits beside the code it is about (§4's table).

---

## 16. Additions of 2026-09-14, and what must not be undone

Built against `docs/PROJECT_AUDIT_2026-09-14.md`. Each line is a property a
later change could quietly break.

- **Setup page never shows a secret.** `tests/test_setup_status.py` runs the
  real vault and inbox checks with sentinel values and asserts neither appears.
- **Nothing contacts an employer.** Tasks are local, reminders use the owner's
  own notification backends, and `.ics` files carry no contact details.
- **Backups never contain the vault key**, exclude browser profiles by
  default, and verification refuses the live database and any directory
  overlapping `storage/`.

The other six moved on 2026-10-26, word for word: registry repair and
posting retention to `packages/crawler/CLAUDE.md`; unknown values, search
preferences, merging, and base versus personalized scores to
`packages/matching/CLAUDE.md`.

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

---

## 28. A company a worker cannot read is noted, then looked into

A worker notes a company it cannot read and moves on (`company_issues`). A
deep dive looks into the notes **only when the owner says so** (`make
deep-dive`; `DEEP_DIVE_AUTO` for standing permission), and runs once nothing
else is waiting, discovery included. What it cannot fix is the error list
(`make company-issues`).
What it tries and why is in `packages/crawler/CLAUDE.md`.

---

## 29. A task's life after it is claimed

Retries back off by kind, a pending task can be cancelled (`/queue`), a
handler past its kind's time limit is stopped, a task records when it
finished, and finished tasks are cleared after a week (failures after a
month). What must not be undone is in `packages/core/CLAUDE.md`.
