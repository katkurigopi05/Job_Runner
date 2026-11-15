# Job Runner runtime audit — 2026-09-14

Local runtime checks plus code inspection. Feature priorities reflect this installation, not the stale capability counts in the backlog.

## Runtime result

- Existing API and PostgreSQL healthy; schema at head `7a1c94b8e3d2`, no migration drift.
- Started the production dashboard at http://127.0.0.1:3001 and worker `jobrunner-audit` with `AUTO_SUBMIT=false`. Queue had no pending work.
- Python lint/format passed: 361 files. Mypy passed: 112 source files.
- Frontend TypeScript and production build passed. Standalone frontend lint failed because ESLint has no configuration file.
- Chromium loaded all 11 main pages, plus an application detail and its apply page, with HTTP 200. Main-page checks found no JavaScript exceptions. Matches, review, and tracker fit a 390px mobile viewport without horizontal overflow.
- Dashboard API proxy healthy. MCP initialized, exposed 24 tools, and successfully called `supported_ats` through the real API.
- Full database-backed pytest suite: **2,632 passed, 2 skipped in 319.90 seconds**, with no errors or failures. Re-ran both skipped noun-phrase checks with `REQUIRE_CHUNKER=1`: **2 passed in 1.80 seconds**. All 2,634 collected tests therefore passed across these two runs.
- Matching benchmark completed on 32 fixture labels. It refused a production recommendation: too little real evidence, with overlapping intervals between leading variants.

No live employer forms were submitted, recruiter messages sent, or paid LLM calls requested. Browser form automation, crawling, tailoring, and inbox logic are exercised by the test suite; this audit does not establish live ATS submission success or mailbox integration.

## Fix before adding features

1. **Frontend verification gap.** `npm run lint` exits 2: `ESLint couldn't find an eslint.config.(js|mjs|cjs) file.` Add the config and a frontend CI job; current CI has no Node setup or dashboard lint/type/build step. Evidence: `apps/web/package.json`, `.github/workflows/ci.yml`.
2. **Vault configuration.** `make doctor` reports an invalid Fernet key. Resolve against any existing encrypted credentials before replacing it; replacing a key does not recover old ciphertext. No secrets were changed.
3. **Inbox setup.** Mailbox unconfigured; zero inbound messages. Reply tracking exists but cannot ingest this user's mail yet.
4. **Registry reconciliation.** All 119 company rows have `source_status=no_website`, no `careers_url`, and no `slug`, while 11,851 open postings exist. The dashboard consequently shows zero boards found and 119 requests for URLs. Inspect curated seeds and the existing `make registry-sync` path before treating this as failed extraction. No registry mutation was made.
5. **Documentation drift.** `docs/BACKLOG.md` still calls recruiter scoring absent, although `packages/tailor/recruiter.py` and the review panel implement it. MCP tool descriptions also describe some implemented phases as future work.

## Recommended features, in order

| Priority | Addition | Why this installation needs it | Acceptance check |
| --- | --- | --- | --- |
| 1 | Setup and recovery page | Doctor results and registry repair currently require a terminal. Existing status panels report problems but cannot guide users through resolving them. | Show vault/inbox readiness, worker heartbeat, registry mismatch, and specific repair steps; never display secret values. |
| 2 | Structured job requirements and filters | `Posting` has no salary range/currency/pay-period fields or structured required/preferred skills and education. Experience and work-authorization checks already exist. | Filter by normalized compensation and mandatory requirements; keep unknown values explicit and preferred requirements distinct. |
| 3 | Canonical jobs and change history | Current uniqueness is company + external ID. No cross-source canonical entity or posting-version table. | Same requisition found twice appears once; preserve source URLs and show salary/requirements changes over time. |
| 4 | Ranking that learns from feedback | 5,624 matches, seven skips, zero interested decisions, zero owner labels. Decisions are stored but do not personalize the rank score. | Add skip reasons and visible preference adjustments; collect balanced owner judgments through existing `/label`; compare against the base scorer on held-out data before adopting learned ranking. |
| 5 | Recruiter contacts, interview and assessment tasks | Tracker recognizes interviews/assessments and reports follow-up cadence, but has no contact entity or structured deadline/task records. | Attach contact, notes, due date, prep checklist, and local reminders to each application; optionally export calendar events. |
| 6 | Backup and verified restore | No project backup/restore workflow found for DB, résumé files, and application artifacts. | Produce a versioned backup, then verify restoration into a separate database/storage directory. Keep vault/key handling explicit and separate. PostgreSQL documents logical backup through [SQL dumps](https://www.postgresql.org/docs/16/backup-dump.html). |
| 7 | Sitemap discovery for bespoke career sites | Generic extraction reads JSON-LD but lacks a sitemap traversal path. Expand this after repairing registry inputs. | Discover job-detail URLs from sitemap indexes, feed them into existing extraction, retain request budgets and robots rules. Follow the [Sitemap protocol](https://www.sitemaps.org/protocol.html), including index and size limits. |

Next résumé enhancement: show source lines and a short reason for each rewrite. The fabrication guard already checks support; the missing part is reviewable per-claim evidence and explicit fact/inference status.

Do not prioritize another model/provider integration yet. Existing labeling, ATS/recruiter scoring, résumé comparison, notifications, follow-up reporting, and four application adapters already cover those foundations. The live application history has five rows: one needs review, three require manual completion, and one failed with `site_error`; none is submitted. More live evidence is needed before claiming application success or ranking improvement.

## Evidence artifacts

The test log, JUnit report, browser results, screenshot and ranking benchmark
behind this audit were kept on the auditing machine and are not in the
repository. This report contains aggregate counts only.
