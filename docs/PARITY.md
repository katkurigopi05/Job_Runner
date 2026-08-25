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
| **A–G rubric → 1.0–5.0** | **HAVE** | `packages/matching/rubric.py`, from the mobile branch |
| Swipe/rate feed | **HAVE** | `/swipe` — and the decisions are the labelled set §15 says is missing |
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
| Cover letter (`cover-letter`) | **HAVE** | `packages/tailor/cover.py`, called from `apply_job::_cover_letter` — written only when the form asks, refusals recorded |
| Voice/style capture (`voice-dna`) | **BUILD** | nothing models the owner's writing voice |
| CV visual regression (`test:cv-visual`) | **BUILD** | we test the parse round-trip, never how it *looks* |
| Tailoring done ahead of time | **HAVE** | `make tailor-batch` — per *posting*, not per company; see below |
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
| Golden-set evals (`eval:golden`) | **HAVE** | `make eval-tailor` — 12 real crawled postings; first run: 57% refused, 11% uptake |
| **Health check** (`doctor`) | **HAVE** | `make doctor` — found a broken `VAULT_KEY` on its first run |
| Pipeline verify | **PARTIAL** | gates cover it; no single command |
| Go TUI dashboard | **DECLINED** | the web app is the surface |
| Self-updater, plugin registry | **DECLINED** | single-user local tool |
| OpenRouter provider | **BUILT** | owner asked; free route, so §11 holds. Opt-in by name only — not in `QUALITY_ORDER`, because §2.8 cannot name the upstream |
| Paid provider runners (OpenAI) | **REFUSED** | §3 — no paid service without asking |

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
3. **Explainable scoring** — rubric landed via mobile. Still open: salary gap,
   and the calibration problem below.

   **The threshold is the live defect.** The first real scoring run over 10,922
   postings produced a maximum of **0.271** against a shipped
   `min_match_score` of **0.75** — unreachable, so nothing ever clears it.
   `/matches/calibration` now derives the number from the owner's own swipes.
   Stripping the HTML out of the postings moved the maximum only to 0.282, so
   the 0.1–0.3 band is what this embedding does, not a bug to fix.
4. **Reach** — company→board resolver, more extractors, board health re-checks.
5. **Documents** — ~~cover letter (finish #32)~~ **done**; visual regression,
   voice capture still open.

   The letter is wired into the apply pipeline: written only when the form
   actually asks for one, vetted by the fabrication guard, stored, and reused
   on a resumed run so the owner and the employer see the same letter. Doing
   it surfaced the defect that made the module refuse most real letters —
   `vet` re-judged the greeting `write` had just stripped, so "Dear Hiring
   Manager," failed on `Manager`. Nothing caught it because every test built
   letters with no greeting. CLAUDE.md §15 has the detail.

   **A note on "per-company caching", which CLAUDE.md §15 lists as a gap.**
   Measured against the real feed, only **5%** of matched postings share an
   identical job description — different roles at one company have different
   descriptions, so reusing a Fivetran backend tailoring for a Fivetran sales
   role would attach a mismatched résumé. The safe unit is the posting, which
   is what `packages/tailor/batch.py` keys on. The §15 entry is worth
   rewording rather than implementing.
6. **Interview material** — story bank, contacts, assessment log.
