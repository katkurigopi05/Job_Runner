# Tsenta — Architecture Teardown + Local Rebuild Plan

Reverse-engineered from public marketing site, docs, and developer API reference.
Internal implementation not public. Everything below marked **[FACT]** (stated by them)
or **[INFER]** (deduced from behavior/constraints).

---

## 1. What Product Actually Does

One sentence: **background agent that watches employer career pages, matches new postings
to a candidate profile, rewrites resume per posting, drives the ATS web form, submits,
then routes recruiter replies back to the application record.**

Four-stage pipeline **[FACT]**:

| Stage | Job |
|-------|-----|
| find | crawl 50,000+ career pages, detect new postings, score match vs profile |
| prep | tailor resume + cover letter from job description, show diff to user |
| apply | drive real ATS form (login, fields, open-ended questions, upload), submit |
| track | ingest recruiter email, route to correct application, move status |

Business model: pay per **submitted** application. Failures free. **[FACT]**
Public API price $0.09/application. **[FACT]**

---

## 2. Surfaces (clients)

All hit same backend. **[FACT]**

- Web dashboard (`dashboard.tsenta.com`)
- iOS + Android native apps (bundle `com.tsenta.tsenta`, deep link `tsenta://`)
- Chrome extension (detect job page, autofill)
- iMessage + WhatsApp conversational agent
- MCP server + CLI (Claude Code, Codex, any tool-speaking agent)
- Public REST API (`https://api.autojobs.me/v1`)

Key architectural read **[INFER]**: marketing domain `tsenta.com` is separate deploy
from API domain `autojobs.me`. Original product name likely AutoJobs, rebranded.
Landing page is static/SSR Next.js (`next-size-adjust` meta tag = Next.js). **[INFER]**

---

## 3. Public API Surface (best signal for internal design)

Base `https://api.autojobs.me/v1`, bearer key `sk_live_...`. **[FACT]**

| Method | Path | Purpose |
|---|---|---|
| GET | `/ats` | supported ATS catalog |
| POST | `/detect` | identify ATS behind posting URL |
| POST | `/candidates` | create candidate from profile + resume |
| POST | `/profiles` | reusable profile for candidate |
| GET | `/profiles`, `/profiles/{id}` | list / one + its runs |
| POST | `/applications` | apply (holds credit, queues work) |
| GET | `/applications`, `/applications/{id}` | list / one |
| POST | `/applications/{id}/review` | approve or reject parked application |
| POST | `/applications/{id}/otp` | supply verification code |
| GET | `/usage` | counts + spend |

### Core object

```json
{
  "id": "9f1c8a2e",
  "candidate_id": "cand_8f2a",
  "profile_id": "prof_4c1d",
  "ats": "lever",
  "url": "https://jobs.lever.co/acme/123/apply",
  "status": "queued",
  "failure_reason": null,
  "price_usd": 0.09,
  "review": null,
  "created_at": "2026-08-06T12:00:00.000Z",
  "updated_at": "2026-08-06T12:00:04.000Z"
}
```

### State machine

```
queued ──> running ──> submitted   (terminal, charged)
             │  ▲
             │  └── needs_review ──> (approve) running
             │                  └──> (reject)  failed[rejected_at_review]
             │
             ├── needs_otp ──> (otp supplied) running
             └────────────────> failed  (terminal, not charged)
```

Failure reasons **[FACT]**: `job_closed`, `unsupported_site`, `incomplete_candidate`,
`manual_completion_required`, `rejected_at_review`, `site_error`.

Error envelope **[FACT]**: `{ "error": { "code", "message" } }` with codes
`unauthorized` 401, `invalid_request` 400/413/422, `not_found` 404,
`insufficient_credit` 402, `rate_limited` 429, `duplicate_application` 409,
`invalid_state` 409, `internal_error` 500.

Webhooks **[FACT]**: `application.running`, `.needs_review`, `.needs_otp`,
`.submitted`, `.failed`. Signed, at-least-once, dedupe key, handler must be
idempotent, redirects rejected not followed.

Rate limits **[FACT]**: 100/min applications, 100/min review, 30/min candidate+profile
creation, 600/min everything else.

---

## 4. Inferred System Architecture

```
┌───────────── CLIENTS ─────────────────────────────────────┐
│ web  ios  android  chrome ext  iMessage/WhatsApp  MCP/CLI │
└───────────────────────┬───────────────────────────────────┘
                        │ REST + webhooks
┌───────────────────────▼───────────────────────────────────┐
│ API GATEWAY  auth (bearer/session), rate limit, quota,    │
│              credit hold, idempotency keys                │
└───┬──────────────┬──────────────┬─────────────┬───────────┘
    │              │              │             │
┌───▼────┐  ┌──────▼──────┐  ┌────▼─────┐  ┌────▼────────┐
│PROFILE │  │  MATCHING   │  │  TAILOR  │  │  TRACKER    │
│SERVICE │  │  SERVICE    │  │  SERVICE │  │  SERVICE    │
│resume  │  │ embed+rules │  │  LLM     │  │ email in,   │
│parse,  │  │ score       │  │ rewrite  │  │ classify,   │
│vault   │  │             │  │ +diff    │  │ route,      │
└────────┘  └──────▲──────┘  └────┬─────┘  │ status move │
                   │              │        └────▲────────┘
            ┌──────┴──────┐       │             │
            │  CRAWLER    │       │        ┌────┴────────┐
            │  50k career │       │        │ INBOUND MAIL│
            │  pages,     │       │        │ managed mbox│
            │  diff feed  │       │        └─────────────┘
            └──────┬──────┘       │
                   │              │
              ┌────▼──────────────▼─────────────────────┐
              │  JOB QUEUE  (durable, retry, priority)  │
              └────────────────┬────────────────────────┘
                               │
              ┌────────────────▼────────────────────────┐
              │  APPLY WORKERS  (headless browser pool) │
              │  per-ATS adapter, form filler, uploader │
              │  OTP handler, screenshot receipt        │
              └────────────────┬────────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │ 29 ATS TARGETS      │
                    │ Workday, Greenhouse,│
                    │ Lever, Ashby, iCIMS,│
                    │ SuccessFactors ...  │
                    └─────────────────────┘
```

### Component notes

**Crawler.** 50k career pages **[FACT]**. Not one-shot scrape — needs change detection.
Store content hash per page, poll on schedule, emit only new posting IDs. Speed is the
product ("first 100 applicants"), so poll interval is competitive advantage. **[INFER]**

**ATS detection.** `POST /detect` **[FACT]** = URL pattern + DOM fingerprint classifier.
Cheap: most ATSes have distinct hostnames (`jobs.lever.co`, `boards.greenhouse.io`,
`*.myworkdayjobs.com`, `jobs.ashbyhq.com`).

**Apply workers.** Real browser automation, not API partnerships — proven by:
"no form mappings to maintain", `site_error` failure reason, per-employer Workday
accounts, OTP flow, "review before submit fills form completely then holds". **[INFER
from FACTs]** Playwright/Puppeteer worker pool, one adapter class per ATS, shared
question-answering layer.

**Question answering.** Unmapped employer question is answered from candidate profile;
if unanswerable, application **pauses and returns exact question** rather than
guessing. **[FACT]** That is a first-class product decision — do the same.

**Credential/identity handling.** Workday needs a real account per employer, so
`workday_password` supplied at candidate creation, stored encrypted, never returned,
never logged. **[FACT]** Secrets vault required, not a DB column.

**Managed email.** `email_mode: managed | candidate` **[FACT]**. Managed = Tsenta-owned
address per candidate so verification codes land somewhere readable and recruiter replies
can be auto-routed. This is what makes the tracker stage work at all. Candidate mode =
they can't read the inbox, so OTP must be supplied via API.

**Credit engine.** Credit **held** at `queued`, **charged** on `submitted`, **released**
on `failed`/rejection. **[FACT]** Classic reserve/capture ledger.

---

## 5. Data Model (rebuild target)

```
User
  id, email, auth, plan, credit_balance, created_at

Candidate
  id, user_id, name, email, email_mode(managed|candidate),
  managed_inbox_address, secrets_ref, created_at

Profile                      # reusable answer set
  id, candidate_id, label, base_resume_ref, phone, location,
  work_auth, needs_sponsorship, links{linkedin,github,portfolio},
  salary_expectation, answers_kv{}, created_at

Resume
  id, candidate_id, version, storage_ref, parsed_json, is_default

Company
  id, name, domain, careers_url, ats_type, logo_ref

Posting
  id, company_id, ats_type, external_id, url, title, location,
  description_raw, description_embedding, first_seen_at, closed_at

Match
  id, profile_id, posting_id, score, reasons_json, created_at

Application
  id, candidate_id, profile_id, posting_id|url, ats,
  status, failure_reason, price_usd, review_json,
  tailored_resume_ref, cover_letter_ref, receipt_json,
  created_at, updated_at
  UNIQUE(candidate_id, url)          # duplicate_application 409

ApplicationEvent                     # append-only audit
  id, application_id, type, payload_json, at

CreditLedger
  id, user_id, application_id, kind(hold|capture|release|topup),
  amount_usd, at

InboundMessage
  id, candidate_id, application_id?, from, subject, body,
  classification(reply|reject|interview|noise), at

WebhookEndpoint / WebhookDelivery
  ..., signature, attempt, dedupe_key
```

Indexes that matter: `Posting(first_seen_at DESC)`, `Application(status)` for worker
pickup, `Match(profile_id, score DESC)`, vector index on `description_embedding`.

---

## 6. Local Rebuild — Scope Decision

Full clone is not one project. Cut to a **single-user local agent** that proves the
whole loop on 2 ATSes. Skip: billing, multi-tenant, mobile apps, iMessage, 50k crawler.

### Recommended stack

| Layer | Pick | Why |
|---|---|---|
| API | FastAPI (Python) | fast, typed, same lang as LLM/parsing tooling |
| DB | Postgres + pgvector | relational + embedding match in one engine |
| Queue | Redis + RQ, or Postgres SKIP LOCKED | durable, retry, simple locally |
| Browser | Playwright (Python) | best selector engine, video/trace for receipts |
| LLM | Anthropic API | tailoring, question answering, reply classification |
| Storage | local `./storage` (S3 interface later) | resumes, PDFs, screenshots |
| Frontend | Next.js + Tailwind | dashboard, diff view, review queue |
| Mail | IMAP poll on one mailbox + `+alias` addressing | managed-inbox equivalent |

Monorepo:

```
tsenta-local/
├── apps/
│   ├── api/            FastAPI, routers mirror /v1 shape
│   ├── worker/         queue consumer, browser pool
│   └── web/            Next.js dashboard
├── packages/
│   ├── core/           models, schemas, state machine, ledger
│   ├── ats/            adapters — base.py, greenhouse.py, lever.py, ashby.py
│   ├── crawler/        career-page pollers + change detection
│   ├── tailor/         resume rewrite + diff generation
│   └── inbox/          IMAP ingest + reply classification
├── docker-compose.yml  postgres, redis
└── ARCHITECTURE.md
```

### ATS adapter interface (the load-bearing abstraction)

```python
class ATSAdapter(Protocol):
    name: str

    @staticmethod
    def matches(url: str) -> bool: ...

    async def parse_posting(self, page) -> Posting: ...

    async def enumerate_fields(self, page) -> list[Question]: ...
        # kinds: text, textarea, email, phone, url, single_select,
        # multi_select, radio, checkbox, boolean, date, file,
        # cover_letter, typeahead, hidden, display

    async def fill(self, page, answers: dict) -> FillReport: ...

    async def submit(self, page) -> Receipt: ...
```

Everything ATS-specific lives behind this. Question kinds copied from their public
review schema **[FACT]** — good taxonomy, no reason to invent another.

---

## 7. Build Phases

**Phase 0 — skeleton (1 week)**
docker-compose up Postgres+Redis. FastAPI with `/candidates`, `/profiles`,
`/applications`. State machine + event log + ledger with fake pricing. No browser yet;
worker just sleeps then flips to `submitted`. Prove queue, states, webhooks, idempotency.

**Phase 1 — one ATS end to end (1–2 weeks)**
Greenhouse first: simplest DOM, no login, stable selectors. Playwright worker:
`parse_posting` → `enumerate_fields` → `fill` → `submit` → screenshot receipt.
Then Lever. Add `POST /detect` from URL patterns.

**Phase 2 — profile + resume (1 week)**
Resume upload, parse to structured JSON, profile answer store. Feed real answers into
`fill`. Any unmapped question → status `needs_review` with exact question returned.
This is where product quality lives.

**Phase 3 — tailoring (1 week)**
LLM rewrite of resume bullets against job description. Hard constraint: **only facts
already present in source resume**. Generate diff (`-old` / `+new`), render to PDF,
require approval before send. Cover letter same path.

**Phase 4 — discovery (1–2 weeks)**
Crawler for a hand-picked list of ~50 companies (not 50k). Per company: careers URL,
ATS type, poll interval. Content-hash diff to emit new postings only. Embed job
descriptions, embed profile, cosine score, plus hard filters (location, seniority,
work authorization). Match feed in dashboard.

**Phase 5 — tracker (1 week)**
IMAP poll on one mailbox using `you+app<id>@gmail.com` alias per application so replies
self-route. LLM classify inbound: interview / rejection / info request / noise.
Auto-move application status. Pipeline board in UI.

**Phase 6 — MCP server (2–3 days)**
Wrap API as MCP tools: `search_jobs`, `tailor_resume`, `apply`, `check_status`.
Now drivable from Claude Code. Small effort, high leverage — this is the fun part.

---

## 8. Hard Problems (plan for them, they will bite)

1. **Selector rot.** ATS DOM changes silently. Need per-adapter smoke tests running on
   real postings nightly, and a `site_error` bucket you actually triage.
2. **Bot detection.** Cloudflare, reCAPTCHA, Workday login walls. Residential proxies,
   human-like timing, and a `manual_completion_required` escape hatch. Do not pretend
   this is solved.
3. **File upload.** Each ATS wants different resume format/size. Normalize to PDF, cap size.
4. **OTP.** Managed inbox is the only clean solution; otherwise application parks.
5. **Idempotency.** At-least-once queue + real form submission = duplicate applications.
   Unique key on `(candidate_id, url)` and a pre-submit existence check.
6. **Hallucinated resume claims.** Tailoring must be extraction-and-rephrase, never
   invention. Test for it explicitly — a fabricated skill on a real application is the
   worst failure mode in the whole system.

---

## 9. Legal / Ethical Constraints — Read Before Building

Not optional, and they shape the architecture:

- **ATS Terms of Service.** Most explicitly prohibit automated submission. Tsenta runs
  this commercially, but that is their risk to carry, not precedent that it is permitted.
  For a local project, apply only to postings **you personally intend to pursue**.
- **robots.txt and crawl rate.** Respect both on career-page polling. Aggressive polling
  is how you get IP-banned and how the target company eats your load.
- **Truthful applications.** Tailoring may rephrase; it may not fabricate. Work
  authorization and employment history answers must be exactly true — misrepresentation
  there has real legal consequences for the applicant.
- **Recipient burden.** Volume auto-apply pushes cost onto human recruiters. Quality
  filter (high match threshold) is the ethical version; spraying is not.
- **Credential storage.** Real ATS account passwords means a real secrets vault, envelope
  encryption, no plaintext logging. Do this in Phase 0, not later.
- **Personal data.** Resumes are PII. Local-only storage, encrypted at rest,
  deletion path.

---

## 10. Open Questions For Us

- Full clone, or personal-use agent for your own search?
- Which ATSes matter for your target roles? (drives adapter priority)
- Approval-required on every send, or auto-send above a match threshold?
- Local-only, or eventually deployed?

Answer these and Phase 0 scaffold gets written next.
