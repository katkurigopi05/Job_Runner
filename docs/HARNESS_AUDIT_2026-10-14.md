# Jobrunner harness, data and safety audit — 2026-10-14

Scope: read-only. No production writes, no crawl started, no application
touched, no dependency installed, no cloud call. Live-database queries ran
inside `BEGIN READ ONLY` transactions. Tests ran against `jobrunner_test`.
Reproductions ran from the scratchpad, not the repo. The working tree's
uncommitted changes (crawl status / spider, five files) were not modified.

Status words used below:

- **Live**: implemented and exercised against the owner's data.
- **Fixture-only**: implemented, tested only with fixtures or recordings.
- **Unwired**: implemented, not reachable from the owner's workflow.
- **Off**: experimental or disabled by default.
- **Missing**: proposed or absent.
- **Excluded**: deliberately out of scope (CLAUDE.md §2, §11).
- **Unverified**: could not be checked from here.

---

## Phase status (handoff log)

Kept current at the end of every phase, so a session that has to stop can be
picked up by another. Each phase is built, tested, pushed and reported before
the next one starts. Phases are in section H.
Phases 2 to 4 each add their own report under `docs/harness-audit/`, so
the open PRs do not edit one shared file; this table is updated by the last.

| Phase | Branch / PR | State | What is left |
|---|---|---|---|
| 1. Submission truth, test-database isolation | #164 `fix/unconfirmed-submit-is-uncertain` | Merged 2026-10-15 | Restart workers on the merged code (`make workers n=…`); the pool was stopped on 2026-10-15 07:42 UTC |
| 2. Local network boundary | #165 `fix/local-network-boundary` | Done, open; report `docs/harness-audit/phase-2.md` | Merge. Owner: where private files live (iCloud); a non-superuser database role |
| 3. Status truth and reconciliation | #166 `fix/status-truth-and-reconciliation` | Done, open; report `docs/harness-audit/phase-3.md` | Merge. Owner: settle the 5 fences on `/finish`; `NOTIFY_BACKENDS=desktop` |
| 4. Audit attribution, derived-data versioning | `fix/audit-attribution` | Done; report `docs/harness-audit/phase-4.md` | Merge |

**Phase 1 report (2026-10-15).**

- **Defect F.2 is fixed.** `packages/ats/form.py::submit_form` returns
  `submitted=True` only when the click produced confirmation text that was
  not on the page before it. `_submit` already parks `submitted=False` as an
  `uncertain` fence, and it now logs `submit_unconfirmed`.
- **Defect F.3 is fixed.** `tests/conftest.py` refuses, through
  `pytest_configure` and before connecting, a `TEST_DATABASE_URL` naming the
  same host, port and database as `DATABASE_URL`. The `engine` fixture refuses
  a database whose comment is not `jobrunner:test`, before `drop_all`. The
  owner's `jobrunner_test` was marked; the live `jobrunner` was not.
  `scripts/init-db.sql` and CI mark the test databases they create.
- **Tests.** Every new test failed before its fix, and each guard was
  mutated out and caught. One existing test, `test_apply_integration.py`,
  had passed only because of the defect: its fake employer never confirmed.
  Its fixture now answers the form POST with a confirmation page.
- **Not done in this phase:** anything about the network, the spider, the
  audit trail.

## A. Executive verdict

**What is real.** The crawler, discovery, matching, the feed, the dashboard
assistant and the MCP server all run on the owner's data. A 15-claimant worker
pool has been live since 2026-10-14 21:37 UTC. There are 12,769 open postings
and 168,829 chunk vectors (0 stale, 0 missing). An exact top-200 vector scan
takes 45–147 ms, so no ANN index is needed. The submission ledger is migrated
(`b8f4c2d6e901`), and its 5 legacy rows are fenced `uncertain`.

**What has never run for real.** No application has ever been submitted. The
5 applications date from 2026-08-20..27: 4 failed (3 at a captcha) and 1
waiting in `needs_review`. The submit step, the fence's post-click branches
and the OTP path have run against fixtures only. The inbox has never read a
real email: IMAP is not configured, and `inbound_messages` has 0 rows. There
are 0 owner relevance grades, so no ranking-quality claim is possible.

**What is unsafe, in order (each confirmed unless marked):**

1. **The live Postgres listens on every interface** (`*:5433`), with the
   compose default `jobrunner`/`jobrunner` **superuser**. The macOS firewall
   auto-allows signed software, Docker included. Anyone on the same network who
   can reach the port gets full read and write access: PII, and the ability to
   set `owner_approved` and queue an `apply` task, which the worker would then
   submit. A superuser can also run `COPY … PROGRAM` inside the container. The
   API and dashboard bind loopback carefully; the database does not. *(The
   bind, the credentials and the firewall setting are confirmed. A connection
   from a second machine was not tried.)*
2. **A click with no confirmation is recorded as `submitted`.**
   `packages/ats/form.py:296` returns `Receipt(submitted=True)` after any
   click plus `networkidle`, whether or not a confirmation appeared. The fence
   then becomes `submitted` and the job is locked for good. Reproduced offline
   against all four adapters' fixture forms. The site rejected the click
   client-side, and the result was `submitted=True confirmation=None` for
   greenhouse, lever, ashby and workable. An invisible captcha or a
   validation error that appears after the click reads as a successful send.
   The owner is told the application went out when it did not.
3. **Nothing stops the test suite from dropping the live schema.**
   `tests/conftest.py` takes `TEST_DATABASE_URL` from the environment or
   `.env`, then runs `drop_all`, `create_all` and `TRUNCATE` on it. It never
   compares that URL with the live `DATABASE_URL` and requires no positive
   proof that the target is a test database. The advisory lock only stops two
   pytest runs from overlapping. One typo (`…5433/jobrunner`) wipes every
   application, the ledger and the registry. *(Confirmed by reading the code.
   Not executed, since executing it is the harm.)*
4. **The whole installation sits in an iCloud-synced folder.** `~/Desktop`
   carries the `com.apple.CloudDocs.iCloudDriveFileProvider` domain attribute,
   and iCloud conflict copies (`… 2.py`, `… 2.md`) were created on 2026-10-13.
   `storage/` (résumés, filled-form screenshots, 11 MB of persistent ATS
   browser sessions), `backups/` (6 DB dumps), `.secrets/` (vault ciphertext
   and LLM audit) and `.env` (`VAULT_KEY` and three provider keys) are all
   inside the repo. That breaks §2.7 ("`.env` outside the repo") and §2.8
   ("local storage only"). It also puts the vault key in the same synced tree
   as the ciphertext it protects. *(Owner to confirm that Desktop & Documents
   sync is on.)*
5. **The API and dashboard check neither `Host` nor `Origin`.**
   `LocalhostOnlyMiddleware` checks only the socket peer. A DNS-rebinding page
   in the owner's browser is a loopback peer with a same-origin view, so it
   could call `POST /applications/{id}/review {"approve": true}`. That
   releases a submission without the owner. FastAPI's JSON content-type rule
   does block plain CSRF. *(Code-level risk. Not reproduced. How exposed this
   is depends on the browser's Private Network Access support.)*
6. **The crawler spider can animate forever.** This affects the uncommitted
   change as well as `main`. A `running` crawl or company task left behind by
   a dead worker, with its lease expired 2 hours earlier, reports
   `running: true, stalled: false`. Reproduced with a scratch test on the
   repo's fixtures, for both "no heartbeat" and "stopped heartbeat".

**Unverified:** whether the 5 legacy `uncertain` fences were actually sent
(they need reconciliation at the employer); Claude Code's own approval dialog
for `ASKS_THE_OWNER` tools; and how the local model performs on tailoring.

---

## B. Current architecture, with code references

| Path | Stages (code) | Loop owner | Status | Evidence |
|---|---|---|---|---|
| **A** CSV → registry → discovery → crawl → storage | `crawler/company_csv.py` (`make import-csv`, offline) → `import_portals.append_to_registry` → `crawler/registry.sync_registry` → `worker/crawl_job.py` tick → `crawler/dispatch.py` → `worker/discover_company_job.py` (`discover.py`, `find_boards.py`, `duplicate_boards.holder_of`, `uq_companies_verified_board`) and `worker/crawl_company_job.py` → `crawler/crawl.py` through `fetch.PoliteFetcher` (`robots_rules.py`, `ratelimit.py`, `host_budget.py`) → `extract.py`, `jsonld.py`, `workday.py` → `postings`, `posting_versions`, `crawl_runs`, `company_crawl_states` | worker pool | Live | 4,382 companies: 689 verified (GH 339, Ashby 239, Lever 91, Workable 20) and 3,693 `failed`, all with a retry scheduled (6/24/72/168 h backoff, uncapped count, by design). 260 runs completed, 0 open. 29,228 `crawl_company` and 12,095 `discover_company` tasks done |
| **B** posting change → vectors/chunks → matching → feed | `matching/score.py` (`score_and_store`), `embed.py` (`lexical-idf@2`), `chunks.py` (bge-small 500/25 offsets plus `content_hash`), `filters.py`, `locality.area_exclusion`, auto-prune (§16) → `matches` → `routers/matches.py` + `matching/search.py` → `/matches` | worker pool | Live | 12,769 open. 12,768 posting vectors `lexical-idf@2` r1 (1 posting has no text). 168,829 chunks, 0 stale, 0 open postings without current chunks. 15 chunk rows on 2 closed postings (filtered at query). 5,153 matches, 1 profile. No ANN index |
| **C** question → retrieval → context → one completion | `routers/chat.py::prepare`: `protected_questions.is_refused` → `asks_to_crawl` → provider check → `_context` + `matching/retrieve.py` (keyword pool, area, role table, RRF with vector ranks; reranker and semantic candidates off) → `llm/provider.py`, one call → `cited_labels`, `unsupported_citations` | **none**: one request, no tools | Live | Local Qwen3 8B by default. The audit shows 74 remote `assistant.system` calls, all owner-picked per question |
| **D** MCP → API | `apps/mcp/server.py` (35 tools) → `apps/mcp/client.py` (HTTP, `_id` path validation) → FastAPI routes | **the MCP client's model** (Claude Code) | Live, enabled on this machine | `.claude/settings.local.json` enables the server (`apps/mcp/CLAUDE.md` still says it is off: stale). The approve, OTP and crawl elicitations were exercised by the library client in tests only |
| **E** apply → tailor → fill → review → fence → submit | `POST /applications` → `apply` task → `apply_job.handle_apply` → `state.begin_work` → `_run_pipeline` (goto, parse, `open_application`, enumerate, `screen`, `_tailor`, `_cover_letter`, `build_answers`, `fill`, screenshot) → `_decide` → `needs_review` → `POST /applications/{id}/review` (candidate lock, row lock, `submissions.blocking`) → re-run → `_submit` (fence `attempting` committed before the click) → `adapter.submit` → `submitted` / `uncertain` | worker (deterministic) | Live to `needs_review` (5 apps, Aug 20–27). **Submit has never run live.** Fence covered by `test_submission_ledger.py` | 4 failed (3 `manual_completion_required`, 1 `site_error`), 1 `needs_review`. 5 fences `uncertain`/`legacy` |
| **F** recruiter mail → classification → event | `worker/inbox_job.py` → `inbox/imap.py` (`UNSEEN` in `INBOX` only) → `inbox/classify.py` (rules; `LLMClassifier` never constructed) → `inbox/match.py` alias → `inbox/route.py` → `transition()` | worker | Fixture-only | No `IMAP_*` in `.env`; `inbound_messages` = 0 |
| **G** worker state → status API → dashboard | `worker/run.py` `Heartbeat` → `worker_heartbeats`; `GET /crawl/status` (`routers/crawl.py`, uncommitted rewrite) → `crawler-spider.tsx` (10 s poll); `GET /events/applications` tails `application_events` | worker / API | Live, with the defect in A.6 | 39 heartbeat rows. The current pool's ids are `Mac.attlocal.net-N` on host `Gopis-MacBook-Pro.local`, because `gethostname()` follows the network, so `reclaimed_from_self` mislabels (log-only impact) |

**Which loops exist, and who bounds them.**

- **The worker pool** is deterministic handlers with leases (300 s, renewed
  every 100 s by `_keep_lease_alive`), at-least-once delivery, 3 attempts and
  discovery last. On `lease_lost` the handler keeps running
  (`run.py:126`), even though `queue.renew_lease`'s docstring says the caller
  should stop. The ledger makes this safe for the click itself.
- **The dashboard assistant is not an agent.** It makes one completion, and
  the model has no tools.
- **The MCP client's loop is the only model-driven multi-step loop.** Its
  step limits, budget and cancellation all belong to Claude Code. Jobrunner
  bounds only what each tool may do.
- **No in-process autonomous agent loop exists.** No agent-run tables or
  states exist either.

---

## C. Database ownership and environment matrix

| Environment | Exists | Resolved target (no credentials) | Writers | Destructive operations | Isolation today | Gaps |
|---|---|---|---|---|---|---|
| **Live** | yes | `localhost:5433/jobrunner`, PG 16.15, pgvector 0.8.6, alembic `b8f4c2d6e901`, 371 MB. Role `jobrunner` is a **superuser**; it is the only role | API, worker pool, `make` scripts (`prune-postings` with `VACUUM FULL`, `merge-duplicate-boards`, registry sync) | prune, merge, VACUUM | Backups refuse it as a restore target | Published on `*:5433`, no least-privilege app role, default password |
| **Test** | yes | `localhost:5433/jobrunner_test`, same server, same superuser, 14 MB | pytest session fixture | `drop_all`, `create_all`, `TRUNCATE` every session | Advisory lock 4917204 (one pytest per DB). `DATABASE_URL` is overridden to the test URL inside pytest | **No refusal when the test URL is the live one. No positive identity marker** |
| **Evaluation** | **no** (proposed only) | — | Eval and bench scripts read the live DB directly today | — | — | Whether every eval script is read-only is unverified script by script. Propose an eval DB only when an evaluation needs to write |
| **Restore verification** | created per run | `jobrunner_restore_<suffix>` on the live server | `packages/backup/verify.py` | create, restore, drop | Positive regex allowlist, refusal of the live name, refusal of storage overlap. 3 verify dirs in `backups/` | Same server and superuser as live (acceptable: the allowlist is positive) |

**What lives outside Postgres** (all of it inside the iCloud-synced repo):
`storage/resumes` (1.1 MB), `storage/receipts` (filled-form screenshots),
`storage/browser` (11 MB, persistent ATS sessions), `storage/logs` (114 MB),
`storage/exports` (103 MB), `storage/embed_rank_bench` (633 MB),
`.secrets/` (vault `*.enc`, `llm-audit.jsonl`, `llm-timings.jsonl`), `.env`,
and `backups/` (6 dumps).

**Authoritative vs derived.** Authoritative: `applications`,
`application_events`, `applied_postings`, `profiles`, `search_preferences`,
`resumes` (rows plus files), `companies` provenance, `posting_labels` (0).
Rebuildable: `postings.description_embedding`, `posting_chunks`, `matches`
scores, `corpus_stats`. Application state and `applied_postings` are in one
database, so no cross-database consistency problem exists, and none should be
created.

**Migrations.** Live and CI run alembic. The test database deliberately uses
`create_all`. `alembic check` is a gate. A second production database is not
justified by anything measured: 371 MB, 45–147 ms scans, one writer pool.

---

## D. Tool permission matrix (35 MCP tools; HTTP API underneath)

| Class | Tools | Owner asked? | Notes |
|---|---|---|---|
| Read-only | `supported_ats`, `search_postings`, `my_matches`, `application_status`, `application_history`, `list_applications`, `review_queue`, `inspect_application_resume`, `follow_ups`, `application_tracking`, `audit_trail`, `setup_health`, `weekly_digest`, `crawl_status`, `next_to_grade`, `grading_progress`, `list_candidates`, `list_profiles`, `list_resumes`, `inspect_resume`, `list_projects` | no | ids go through `_id`. Contact details are cut from the tracking tool. `inspect_*` returns résumé text to the client's model (remote) |
| Read-only compute via POST | `detect_ats`, `preview_resume`, `preview_projects` | no | Local; no provider call |
| Local mutation | `select_tailoring` (changes **which file an employer receives**), `edit_application_resume` (guard on), `curate_project`, `reject_application` (terminal), `grade_posting` (owner form) | grade only | A model can select a document before the owner approves through a form that shows only the questions |
| Outbound | `apply_to_url` (queues; the worker opens the employer page, sends résumé and posting to the tailoring provider, which is Gemini on this machine, and types PII into the form; does not submit), `compare_tailoring` (an extra §2.8 upload to a named cloud), `sync_github_projects` | **no** | A prompt-injected model can cause uploads and form fills, not a submission |
| Sensitive external action | `approve_application`, `submit_otp` | **yes** (`ASKS_THE_OWNER`, `.claude/settings.json` ask rule, owner-typed form) | Fails safe when the client cannot show a form |
| Administrative | `start_crawl` | yes | Registry sync, prune, merge, backups, `/audit/verify`, chat and inbox are not exposed |

Server-side authority: the API cannot tell the dashboard from a model
(already recorded in `apps/mcp/CLAUDE.md`). The client-side `_meta` and ask
rules are the only marks. The server enforces approval through
`approve: true` plus the row lock and the ledger, so a model cannot submit
without the elicitation path, but **any HTTP caller on loopback can**,
which is why finding A.5 matters.

---

## E. Loops and memory: current vs proposed

**Memory, mapped to what already exists. No new table is needed.**

| Kind | Where it lives | Authority | Notes |
|---|---|---|---|
| Working | Chat: last 3 exchanges sent by the dock (`chat_history.py`), page-scoped, never stored. Worker: `review_json` per application | derived, bounded | Truncation is stated (questions at 300 characters, answers at 500) |
| Procedural | CLAUDE.md tree, `.claude/rules`, versioned prompts (`assistant.system` v6, `tailor.system`), code rules (`is_refused`, guard, gate) | authoritative | A model or a page cannot override these: §2.2 is refused in code |
| Semantic | `profiles` (verbatim §2.2 fields), `search_preferences`, `postings` with `first_seen_at`/`last_seen_at`, `companies` provenance; vectors as indexes | profile is owner-confirmed; vectors derived | Nothing writes a profile fact from a conversation. Keep it that way |
| Episodic | `application_events`, `applied_postings`, `queue_tasks`, `crawl_runs`, `company_crawl_states`, `.secrets/llm-audit.jsonl`, `llm-timings.jsonl` | authoritative logs | Audit gaps in F.7 |

**Proposed agent-run harness: not now.** The two loops that exist already
have owners: the worker's, deterministic, and the MCP client's, bounded by
Claude Code. An in-process autonomous loop would add a new place where model
output becomes an action, and no measured gap needs one. If a run record is
wanted later, it should be a thin table keyed to MCP calls (run id, tool,
redacted argument digest, outcome, approval reference), separate from
`ApplicationStatus` and never calling `transition()` itself. The candidate
states (`created → executing → waiting_for_owner → completed / failed /
cancelled / budget_exhausted`) stay a design until there is a consumer.
**No framework** (LangGraph, LangChain, LlamaIndex) answers a gap found here.
Note: `llama_index` is **installed in `.venv` but declared nowhere**
(`pyproject.toml`, `constraints.txt`), and it backs the untracked
`experimental_llamaindex.py` work. A fresh checkout or CI will not have it.

---

## F. Risk-ranked gaps

| # | Gap | Severity | Status | Where | Smallest responsible fix |
|---|---|---|---|---|---|
| 1 | Postgres on `*:5433` with a default-password superuser | Critical on an untrusted network | Confirmed (bind, credentials, firewall auto-allow) | `docker-compose.yml`, `docker-compose.override.yml` | Publish `127.0.0.1:5433:5432` (and `127.0.0.1:5432:5432` in the base file). Then a non-superuser app role and a real password |
| 2 | A click with no confirmation is recorded `submitted` | High (false success, job locked) | **Reproduced**, 4/4 adapters | `packages/ats/form.py:296`, `apply_job._submit` | No confirmation → fence `uncertain`, park `needs_review` with the reason; never `submitted` |
| 3 | The test suite can drop the live schema | High (data loss on a typo) | Confirmed by reading | `tests/conftest.py:76-94, 179-230` | Refuse when the test target equals live by host, port and DB name; require a positive marker on the test DB |
| 4 | Installation, keys, sessions and backups in iCloud | High (privacy, §2.7, §2.8) | Very likely; owner to confirm | `~/Desktop/Job_Runner` | Move the repo out of Desktop, or move `storage/`, `backups/`, `.secrets/` and `.env` outside the synced tree (settings already exist for each root) |
| 5 | No Host/Origin allowlist on API or dashboard | High impact, browser-dependent likelihood | Code-level risk | `apps/api/middleware.py`, `apps/api/main.py`, `apps/web/next.config.ts` | Refuse a `Host` other than `127.0.0.1:<port>` / `localhost:<port>`; check `Origin` on writes |
| 6 | The spider stays "running" on a dead worker's expired lease | Medium | **Reproduced** | `apps/api/routers/crawl.py` (uncommitted) | Count `running` only with an unexpired lease or a live worker; otherwise `stalled` |
| 7 | Audit cannot attribute uploads | Medium (§2.8 auditability) | Confirmed on data | `packages/llm/audit.py` callers | All 14,458 entries are `task=unspecified`. 229 remote calls have no prompt name, 95 of them in the last 14 days (Gemini 50, OpenRouter 44, TokenRouter 1). Pass `task=` at every call site |
| 8 | 5 legacy `uncertain` fences, and no surface for the 4 on `failed` applications | Medium (reconciliation) | Confirmed | `applied_postings` | Owner checks each employer; **mark submitted** or release. Lists them on `/review` or `/setup` |
| 9 | Doorbell unconfigured | Medium (operational) | Confirmed | `.env` has no `NOTIFY_*` → `log` | Owner picks `desktop` or `webhook`. 1 application has waited since Aug 27, 3 need hand-finishing |
| 10 | The handler keeps driving after `lease_lost` | Low–Medium | Code-level | `apps/worker/run.py:126` | Cancel the handler on `lease_lost` (the fence already protects the click) |
| 11 | 60 open postings on 8 failing boards (oldest success 2026-09-18) | Low–Medium | Confirmed | feed, assistant | Show staleness, do not close (a failed fetch is not a verdict) |
| 12 | Chunk stamp is the backend name, not the model id | Low | Confirmed | `matching/embed.py:165` | Stamp `sentence-transformers:BAAI/bge-small-en-v1.5` (needs a one-time restamp) |
| 13 | 12,520 `stub` entries in the live audit trail (2026-08-17 to 09-18, a fixed test leak) | Low | Confirmed | `.secrets/llm-audit.jsonl` | Archive or annotate; the counts are skewed |
| 14 | `llama_index` installed but undeclared | Low | Confirmed | `.venv` | Declare as an optional extra, or uninstall when the experiment ends |
| 15 | Doc drift: "MCP server switched off on this machine" | Low | Confirmed | `apps/mcp/CLAUDE.md` | Update; the elicitation dialog can now be watched live |

Deliberately excluded and kept so: captcha solving, stealth and proxies
(§2.5); Workday forms (§11); a vector database or workflow engine; hosted
tracing.

---

## G. Evaluation matrix: actual results

| Area | What exists | Result here | Limitation |
|---|---|---|---|
| Deterministic safety (submission) | `test_submission_ledger.py`, `test_review_is_decided_once.py`, `test_queue.py` | pass (in the 186 below) | The no-confirmation case is untested and fails (F.2) |
| Prompt-injection, ids | `test_red_team.py`, `test_mcp_ids.py` | pass | Whether a model *obeys* an injection is untestable without a model (by design) |
| Environment isolation | `test_the_suite_owns_its_database.py`, `test_one_run_per_database.py` | pass | They test the override and the lock, not a live-URL collision (F.3) |
| Crawl status | `test_crawl_status.py` (incl. the uncommitted tests) | pass | The expired-lease case fails (scratch reproduction) |
| Adapters | `test_adapter_fill.py` | pass | Fixture HTML. Live Gate 1 is blocked by captcha (correct) |
| Focused run | 9 files | **186 passed, 0 failed, 107 s** | |
| Full suite | `pytest -q -rs`, iCloud ` 2.py` copies excluded | **4,343 passed, 2 skipped, 0 failed, 8 min 41 s** | Skips: `test_chunker_available.py` ×2 (`REQUIRE_CHUNKER` unset). Green suite does not cover findings F.2, F.3, F.6 |
| Retrieval | `make eval-retrieval`, known-target | Not re-run (would read live and take minutes; results in `docs/ML_EVALUATION.md`) | Known-target rank ≠ relevance; 0 owner grades |
| Ranking | `make bench-matching` | Not re-run | Fixture labels only; refuses to name a production candidate (correct) |
| Inbox | Gate 6 fixtures | Not re-run | 0 real emails |
| Fabrication | Gate 3 | In full suite | Fixture résumés |
| LLM judge | none relied on | — | Kept supplementary, as asked |

---

## H. Phased plan

Each phase stops at its gate and waits for approval. Nothing here migrates
the live database except where marked as the owner's step.

### Phase 1: submission truth and test-database isolation *(proposed first)*

- **User-visible outcome.** A send the site did not confirm parks as
  "uncertain: check the employer" instead of reading "submitted". `pytest`
  refuses to start against the live database.
- **Scope.** (a) A missing confirmation after the click becomes `uncertain`.
  (b) `conftest` refuses a test target equal to live, and requires a
  positive marker.
- **Non-goals.** Adapter selector changes, captcha handling, new states,
  the crawl-status change, compose files.
- **Reused.** The fence's existing `uncertain` branch (`apply_job._submit`)
  and the owner's existing **mark submitted** / approve-once-more flow.
  `Receipt.confirmation_text`. `SCHEMA_LOCK_KEY` claim.
- **Files.** `packages/ats/form.py` (or `apply_job._submit`; one place
  only), `tests/test_submission_ledger.py`, `tests/test_adapter_fill.py`,
  `tests/conftest.py`, `tests/test_the_suite_owns_its_database.py`,
  `scripts/init-db.sql` (marker for fresh installs), `apps/worker/CLAUDE.md`
  and `tests/CLAUDE.md` notes.
- **Data / migration.** None for the live DB. The test DB needs one marker
  statement: `COMMENT ON DATABASE jobrunner_test IS 'jobrunner:test'`. It is
  the owner's to run, or the fixture prints the exact command and refuses.
- **Acceptance tests (written first, seen red).**
  1. A form whose click shows no confirmation leaves the fence `uncertain`
     and the application `needs_review` with `submission_uncertain=True`.
     Never `submitted`.
  2. A confirmed click still records `submitted`, then the screenshot,
     unchanged.
  3. With `TEST_DATABASE_URL` equal to the live URL, the session fixture
     fails before any DDL, without printing credentials.
  4. A database without the marker is refused, with the remedy.
  5. Gates 0 and 1 and the full suite stay green.
- **Rollback.** Revert the commit. No data changes.
- **Owner decisions.** (i) Should "no confirmation" be `uncertain` for every
  adapter, or only where a confirmation selector is known? *Recommend:
  every adapter.* (ii) The test-DB marker: comment or table? *Recommend:
  the comment.*
- **Stop condition.** Tests 1–5 green, focused and full suites green, PR
  open. No live run.

### Phase 2: local network boundary *(small, one owner step)*

The Postgres bind becomes `127.0.0.1` in both compose files (container
recreate, workers stopped first: **the owner's step**). The API and
dashboard get a `Host`/`Origin` allowlist. The owner decides where `storage/`,
`backups/`, `.secrets/` and `.env` live (out of iCloud). Then a
least-privilege app role, which is its own change with a migration plan.
Stop: `lsof` shows `127.0.0.1:5433`, a rebinding-shaped request is refused
in tests, and the app is unchanged otherwise.

### Phase 3: status truth and reconciliation

Finish the spider change with the expired-lease rule (coordinate with
whoever owns the uncommitted diff). List the 5 `uncertain` fences for owner
reconciliation. Configure the doorbell. Mark postings on failing boards as
stale. Cancel the handler on `lease_lost`. Stop: the scratch reproduction
becomes a repo test and passes, and the owner has reconciled or kept each
fence.

### Phase 4: audit attribution and derived-data versioning

`task=` at every provider call site. Name every prompt that reaches a remote
provider. Report uploads per task. Stamp chunk vectors with the model id
(restamp in a transaction, verified by count). Archive the stub entries.
Stop: in a fresh run every new audit entry has a task, and §2.8's "one
upload" is countable.

### Later, only on evidence

An eval database, only if an evaluation must write. Owner grades (≥100 across
two streams) before any ranking change. An agent-run record only when there
is a consumer. Frameworks only against a named gap, with the no-framework
option kept.
