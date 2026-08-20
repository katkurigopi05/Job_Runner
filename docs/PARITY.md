# Parity with career-ops

Every capability in <https://github.com/santifer/career-ops> (MIT, ~400
contributors), mapped against this project. The owner's instruction was
"everything they have", so this is the working list rather than a survey — it
gets shorter as things land.

Read the teardown in [REFERENCE.md](REFERENCE.md) §7 first for *why* several
rows say REFUSED. In short: they carry no equivalent of CLAUDE.md §2.6
(robots.txt) or the no-paid-services rule, so some of their code is fine there
and not here.

Their surface was enumerated from `package.json` (57 scripts), `modes/`, and
`providers/` (79 files), cloned and read on 2026-08-19.

---

## Status key

| | |
|---|---|
| **HAVE** | equivalent exists and is tested here |
| **PARTIAL** | some of it exists; the gap is named |
| **BUILD** | missing, wanted, nothing blocks it |
| **REFUSED** | a rule in CLAUDE.md §2 forbids it |
| **DECLINED** | architecture we deliberately did not take |

---

## Discovery

| Their capability | Status | Here |
|---|---|---|
| Portal scan (`scan`) | **HAVE** | `packages/crawler/crawl.py` |
| ATS directory walk (`scan:full`) | **PARTIAL** | 4 extractors against their 79 providers |
| Company → board resolver (`discover-ats`) | **BUILD** | we detect from a URL, not from a name |
| Aggregator ingest | **HAVE** | `packages/crawler/discover.py` |
| Board health (`validate:portals`) | **PARTIAL** | `make validate-seeds` exists; no scheduled re-check |
| Liveness (`liveness`) | **HAVE** | `packages/crawler/liveness.py` |
| Repost detection (`reposts`) | **HAVE** | `packages/matching/legitimacy.py` |
| Funding signal (`company:funded`) | **BUILD** | no funding data held |
| VC-portfolio seeding (`scan:seeds`) | **REFUSED** | `api.ycombinator.com` is `Disallow: /` — §2.6 |
| German public sector (`scan:interamt`) | **DECLINED** | not this owner's market |

## Matching and evaluation

| Their capability | Status | Here |
|---|---|---|
| Semantic JD similarity (`jd:similarity`) | **HAVE** | `packages/matching/embed.py`, `score.py` |
| Skill gap (`jd-skill-gap`) | **HAVE** | `score.py` — what the posting wants and the résumé lacks |
| Legitimacy tiering (`classify-tier`) | **HAVE** | `packages/matching/legitimacy.py` |
| Hard filters | **HAVE** | `packages/matching/filters.py` |
| **A–G rubric → 1.0–5.0** | **BUILD** | ours is cosine + filters; theirs decomposes into named criteria you can argue with |
| Star matching (`star`) | **BUILD** | manual pin/boost of a posting |
| Salary gap (`salary-gap`) | **BUILD** | `Profile.salary_expectation` is held and never compared |
| Upskill plan (`upskill`) | **BUILD** | gap is computed; nothing turns it into a plan |

## Résumé and documents

| Their capability | Status | Here |
|---|---|---|
| Fabrication check (`cv:verify-facts`) | **HAVE** | `packages/tailor/guard.py` — ours is stricter (per-entry scoping) |
| Tailoring (`batch-tailor`) | **HAVE** | `packages/tailor/rewrite.py` |
| PDF render (`pdf`) | **HAVE** | `assemble.py`, `publish.py` |
| HTML CV (`build-cv-html`) | **HAVE** | `assemble_html` |
| LaTeX CV (`build-cv-latex`) | **DECLINED** | WeasyPrint covers it; LaTeX is a second toolchain |
| Cover letter (`cover-letter`) | **PARTIAL** | PR #32 — sentence guard rejects salutations, emits a stub |
| Voice/style capture (`voice-dna`) | **BUILD** | nothing models the owner's writing voice |
| CV visual regression (`test:cv-visual`) | **BUILD** | we test the parse round-trip, never how it *looks* |
| Per-company tailoring cache | **BUILD** | §15 already records this gap |
| Image → PDF (`img-to-pdf`) | **DECLINED** | narrow utility |

## Applying

| Their capability | Status | Here |
|---|---|---|
| Prefill summary (`prepare:application`) | **HAVE** | and further — we fill the live form, they print to stdout |
| Application answers store | **HAVE** | `Profile.answers_kv_json`, `packages/ats/` |
| Artifact bundle (`application:init`) | **HAVE** | `GET /applications/{id}/packet` |
| Archive a posting (`archive`) | **BUILD** | no snapshot of the JD as it read on the day |
| Auto-submit | **REFUSED (both)** | theirs never submits; ours needs §2.3 approval. Four peer projects agree |

## Tracking

| Their capability | Status | Here |
|---|---|---|
| Pipeline/tracker | **HAVE** | `Application` + web dashboard |
| Status normalize/dedup/merge | **HAVE** | state machine + `UNIQUE(candidate_id, url)` |
| Inbound reply matching | **HAVE** | `packages/inbox/` |
| Interview invite detection | **HAVE** | inbox classification |
| **Funnel analysis** (`patterns`) | **HAVE** | `packages/analytics/funnel.py` |
| **Follow-up cadence** | **HAVE** | `packages/analytics/cadence.py` — reports, never sends |
| **Rejection latency** | **HAVE** | `cadence.latency()`, split by outcome kind |
| **Weekly digest** | **HAVE** | `packages/analytics/digest.py` |
| Recruiter contacts | **BUILD** | no contact record |
| Interview prep / story bank | **BUILD** | answers are stored, never accumulated into reusable material |
| Assessment log | **BUILD** | no record of take-homes |
| Negotiation ROI | **DECLINED** | past the point this tool is for |

## Infrastructure

| Their capability | Status | Here |
|---|---|---|
| Web dashboard | **HAVE** | `apps/web/` |
| MCP / CLI-native | **HAVE** | `apps/mcp/` |
| Golden-set evals (`eval:golden`) | **BUILD** | §15 says our gate fixtures are not real material |
| **Health check** (`doctor`) | **HAVE** | `make doctor` — found a broken `VAULT_KEY` on its first run |
| Pipeline verify | **PARTIAL** | gates cover it; no single command |
| Go TUI dashboard | **DECLINED** | the web app is the surface |
| Self-updater, plugin registry | **DECLINED** | single-user local tool |
| Paid provider runners (OpenAI, OpenRouter) | **REFUSED** | §3 — no paid service without asking |

---

## Order of work

Grouped so each block ships something usable rather than a scatter of
half-features.

1. ~~**Tracking intelligence**~~ — **done**. `packages/analytics/`, three
   endpoints, 13 tests. The feedback loop REFERENCE.md §3.5 named as missing.
2. ~~**`doctor`**~~ — **done**. `make doctor`. It earned itself on the first
   run by finding a `VAULT_KEY` that was 91 characters where Fernet needs 44 —
   every credential write would have raised, and nothing had noticed because
   nothing had ever tried to store one.
3. **Explainable scoring** — the A–G rubric, salary gap, star.
4. **Reach** — company→board resolver, more extractors, board health re-checks.
5. **Documents** — cover letter (finish #32), tailoring cache, visual
   regression, voice capture.
6. **Interview material** — story bank, contacts, assessment log.
