# Parallel work split

Four streams that can run at the same time without stepping on each other.

The split is by **file ownership, not by phase**. Phases are already built —
what remains are gaps in different corners of the tree, and the only thing that
makes parallel work painful is two agents editing the same file. So streams 1
and 2 are drawn to touch *nothing* that streams 3 and 4 touch.

Read `CLAUDE.md` first. Everything in §2 is non-negotiable in every stream.

---

## Ownership

| Stream | Owns | Must not touch |
|---|---|---|
| **1 — Adapter** | `packages/ats/**`, `tests/test_greenhouse.py` | `apps/web/**`, `packages/core/schemas.py`, `apps/api/**` |
| **2 — Discovery data** | `seeds/**`, `packages/crawler/**`, `tests/test_crawler.py` | `apps/web/**`, `packages/core/schemas.py`, `apps/api/**` |
| **3 — Tracker** | `packages/inbox/**`, `apps/api/routers/inbox.py`, `apps/web/src/app/tracker/**`, `tests/test_inbox*.py` | `packages/ats/**`, `packages/crawler/**`, stream 4's pages |
| **4 — Matches + diff** | `apps/api/routers/postings.py`, `apps/web/src/app/matches/**`, `apps/web/src/app/review/**`, `tests/test_matching.py` | `packages/ats/**`, `packages/crawler/**`, stream 3's pages |

**Streams 1 and 2 never touch shared files.** Hand those to the other agent —
they are the safe ones to run unattended.

### Shared files

Only streams 3 and 4 touch these, and they must coordinate:

- `packages/core/schemas.py` — **append only**, at the end of the relevant
  section. Never reorder or reformat existing models.
- `apps/api/main.py` — one line each to register a router.
- `apps/web/src/lib/api.ts` — append types and one entry to the `api` object.
- `apps/web/src/app/layout.tsx` — one `NAV` entry each.
- `Makefile`, `.github/workflows/ci.yml` — one gate line each.

If both streams are running, do 3 first and let 4 rebase onto it. Both are
small edits; the conflict is annoying, not dangerous.

---

## Stream 1 — react-select options

**Problem.** Greenhouse renders dropdowns with react-select. The kind is now
detected correctly (`role="combobox"` → `single_select`), but `options` is still
empty, because react-select only renders its menu into the DOM after the control
is opened. The adapter reads `option` elements that do not exist.

**Why it matters.** §2.2 requires the work-authorization answer to be copied
verbatim *and* to match an option the employer offered. With no options list
there is nothing to match against, so `build_answers` cannot map an answer and
the question falls through to `needs_review` every time. Correct, but it means
every application stops on a question the profile already answers.

**Do.** Open each combobox, read the rendered menu, close it. Live markup is in
`tests/test_greenhouse.py::_REACT_SELECT_FORM`; the real menu appears under
`[id^="react-select-"][id$="-listbox"]` with `[role="option"]` children.

**Done when.** `make gate-1` passes, and `make gate-1-live URL=<figma posting>`
shows non-empty `options=[...]` on the work-authorization question.

**Watch out.** Opening a menu is interaction. Do not let it submit anything, and
keep the captcha guard in front of it — §2.5.

---

## Stream 2 — seed slugs

**Problem.** `seeds/companies.yaml` lists 50 companies. Sampling six found four
dead: `linear`, `ramp`, `retool`, `render` all return 404 from both the board
API and the rendered page. `vercel` and `figma` work.

**Why it matters.** Gate 5 cannot catch this. A 404 board yields zero postings,
which is indistinguishable from "nothing new since last poll" — so discovery
silently covers a fraction of the registry it claims to.

**Do.** Validate all 50 slugs, correct the ones with a different Greenhouse
identifier, drop the ones that have left Greenhouse. Then make the crawler
*notice*: a board that 404s should be logged and marked, not treated as an empty
result.

**Done when.** Every slug in the file resolves, and `make gate-5` passes with a
new test proving a 404 board is reported rather than silently counted as zero.

**Watch out.** §2.6 — 60 seconds between requests to the same host, and every
Greenhouse board shares one. Validating 50 slugs takes ~50 minutes of wall
clock. Space the requests; do not parallelize them.

---

## Stream 3 — tracker (Phase 6 UI)

**Problem.** `packages/inbox/` is built — IMAP poll, alias routing,
classification — and has no API router at all. Nothing outside the worker can
see an inbound message.

**Do.** Add `apps/api/routers/inbox.py` exposing inbound messages per
application, then a `/tracker` page: applications as a pipeline board, with the
recruiter replies that moved them.

**Done when.** `make gate-6` passes, and a test email to
`owner+app{id}@gmail.com` shows up on the right application in the dashboard.

**Watch out.** Inbound message bodies are the recruiter's words and may contain
personal detail — §2.8. They stay local, and the API is loopback-only already.

---

## Stream 4 — match feed and diff

**Problem.** Two gate requirements have working backends and no UI.

- §9 Phase 5: "Match feed in the dashboard." `packages/matching/` scores
  postings; nothing displays them.
- §9 Phase 3: "Diff renders in the UI before send."
  `packages/tailor/diff.py` produces the diff; the review screen does not show
  it.

**Do.** A `/matches` page ranking scored postings with the reasons behind each
score, and a diff block in the review card — the tailored résumé against the
source, shown before the approve button.

**Done when.** `make gate-3` and `make gate-5` pass, the feed ranks a
hand-labelled set sanely, and the review screen shows what changed in the
résumé before you approve it.

**Watch out.** The diff is the §2.1 audit surface. It has to show what was
actually sent, not a summary of it — if the guard rejected a rewrite, the
reviewer needs to see the original that went instead.

---

## Rules for both agents

1. **Branch per stream.** `stream-1-adapter`, `stream-2-seeds`, and so on. Never
   commit to `main`.
2. **Rebase before pushing**, every time. `git pull --rebase origin main`.
3. **`make gate-0` must pass before every push.** It runs the whole suite; a
   stream that breaks another stream's tests finds out locally rather than in
   CI.
4. **Small PRs, merged often.** A stream that runs for two days without merging
   will conflict with everything.
5. **Never weaken a §2 rule to make a test pass.** If a non-negotiable is in the
   way, stop and ask — that is the one situation where blocking is correct.

## Order of merge

Streams 1 and 2 are independent of everything and can merge whenever they are
green. Stream 3 before stream 4, because both add a nav entry and a router
registration, and 4 rebasing onto 3 is cheaper than the reverse.
