# Parallel work split

Four streams that can run at the same time without stepping on each other.

The split is by **file ownership, not by phase**. Phases are already built —
what remains are gaps in different corners of the tree, and the only thing that
makes parallel work painful is two agents editing the same file. So streams 1
and 2 are drawn to touch *nothing* that streams 3 and 4 touch.

Read `CLAUDE.md` first. Everything in §2 is non-negotiable in every stream.

---

## Before you start: a worktree of your own

Every agent works in its own git worktree. Not a branch in the shared
checkout — a worktree.

```bash
cd /Users/gopikrishnareddykatkuri/Desktop/Job_Runner
git worktree add /private/tmp/Job_Runner_<stream> -b <branch> origin/main
cd /private/tmp/Job_Runner_<stream>
ln -s /Users/gopikrishnareddykatkuri/Desktop/Job_Runner/.venv .venv
```

The main checkout at `~/Desktop/Job_Runner` belongs to whoever is driving
interactively. An agent that starts editing there shares an index and a working
tree with them: same files, same staged changes, and git offers no protection
because it is one repository. This has already happened once — an agent
reported it was on another agent's feature branch, in that checkout, about to
begin.

**Always run `.venv/bin/python`, never bare `python3`.** The system interpreter
on this machine is 3.14; the project needs the 3.12 venv. An agent that checked
`python -m playwright --version` against system Python reported Playwright
missing when it is installed and working — and would also have failed to run
the test suite, since `make gate-0` uses `.venv/bin`.

## Ownership

| Stream | Owns | Must not touch |
|---|---|---|
| **5 — Workable** | `packages/ats/workable.py`, `registry.py`, `tests/test_workable.py` | `apps/**`, `packages/core/schemas.py`, `packages/tailor/**` |
| **6 — Tailoring** | `packages/tailor/**`, `tests/test_no_fabrication.py`, `tests/test_keywords.py` | `apps/**`, `packages/ats/**`, `packages/core/schemas.py` |
| **Interactive** | `apps/**`, `packages/core/schemas.py`, routers, docs | the other two streams' files |

**Streams 5 and 6 never touch shared files.** Those are the ones safe to hand
to an agent working unattended in its own session.

### Shared files

Neither background stream touches these. They belong to whoever is driving
interactively, and they are listed so an agent recognises when it has wandered
out of its lane:

- `packages/core/schemas.py` — **append only**, at the end of the relevant
  section. Never reorder or reformat existing models.
- `apps/api/main.py` — one line each to register a router.
- `apps/web/src/lib/api.ts` — append types and one entry to the `api` object.
- `apps/web/src/app/layout.tsx` — one `NAV` entry each.
- `Makefile`, `.github/workflows/ci.yml` — one gate line each.

If a stream genuinely needs one of these, stop and ask rather than editing it.
A conflict here costs more than the wait — these are the files every surface
reads, and a botched merge in `schemas.py` breaks the API, the worker, and the
dashboard at once.

---

## Streams 1–4 — done

The first split is finished and merged. Kept here in one line each, because the
reasoning is in the PRs and the value now is knowing what was tried.

1. **react-select options** — Greenhouse renders dropdowns as `input
   role="combobox"` with no `<option>`. Options are read by opening the menu.
   The follow-up fix (`0622cb7`) is the lost-commit story above.
2. **Seed slugs** — 21 of 50 registry entries were dead boards. Removed, with
   `make validate-seeds` to check the rest. A 404 board yields zero postings,
   which reads identically to "nothing new".
3. **Tracker** — `packages/inbox/` was complete and unreachable; it now has a
   router and a `/tracker` board grouped by outcome rather than status.
4. **Match feed and diff** — both were gate requirements with working backends
   and no interface. Tailoring was never invoked at all.

---

## Stream 5 — Workable adapter

**Owns** `packages/ats/workable.py`, `packages/ats/registry.py`,
`tests/test_workable.py`. Touches nothing else.

§8 names four adapters in build order — Greenhouse, Lever, Ashby, Workable —
and the first three exist. Any posting outside them fails as
`unsupported_site`.

**Build it against a live posting, not a fixture written from memory.** This is
not a preference. Every adapter so far turned out structurally different in a
way nobody predicted:

| | |
|---|---|
| Greenhouse | a real form, every dropdown react-select with no `<option>` |
| Lever | a real form, native `<select>` |
| Ashby | **no `<form>` element at all** |

The react-select bug survived a green suite for weeks because its fixture was
hand-written and more polite than the real DOM.

**Done when** `make gate-0` passes and the tests are trimmed from markup that
was actually observed. `fill()` and `submit()` may raise `NotImplementedError`
if they cannot be verified against a live form — an unverified fill path puts
unchecked values on a real application, which is worse than an honest gap.

Expect a captcha. All three existing ATSes mount one at the apply stage; §2.5
says stop, never route around.

---

## Stream 6 — tailoring quality

**Owns** `packages/tailor/**`, `tests/test_no_fabrication.py`,
`tests/test_keywords.py`.

Tailoring is safe and not yet good. `scripts/bench_tailor.py` measured four
local models and found three gaps the guard misses.

**Scope claims.** Models write "Owned high-reliability backend services" and
"Led a team" when the résumé says neither. §2.1 enumerates *skill, employer,
date, credential, metric* — so these pass the letter and break the spirit. A
hiring manager reads "Led" as a claim about scope.

**Morphological variants.** The off-limits check is exact-token. A posting said
`scale`, a model wrote `scalability`, and it passed.

**Cover letter.** §9 Phase 3 lists it. It does not exist.

**Done when** `make gate-0` and `make gate-3` pass and the benchmark shows
fewer accepted-but-wrong rewrites. Report the before/after table — and print
the rewrites, because the numbers mislead on their own: `phi3:mini` currently
scores best on every metric and writes the worst output.

---

## The failure mode that actually bit us

Not conflicts. Git surfaces those loudly and they get resolved.

**Work that quietly does not land.** Commit `0622cb7` — a fix for a race in
react-select option reading, with the regression test that proved it — was
pushed to a branch whose PR had already merged the state before it. The PR
showed as merged. The branch had the commit. `main` did not.

Nobody noticed for days. It surfaced only when someone grepped `main` for a
constant the fix introduced and got nothing back.

**So: after a merge, verify the artifact on `main`, not on your branch.**

```bash
git checkout main && git pull
grep -r "<the thing you added>" <the file you added it to>
```

One grep. It is the only check that distinguishes "the PR merged" from "my work
is in the product", and those are not the same statement.

## Sessions and context

Agents cannot reliably see their own limits. Asked directly, one answered
UNCERTAIN to every limit question while its host was displaying `30% context
left` on the same screen. The telemetry exists; the model has no access to it.

**So the human watches the meter, not the agent.** Practical rules:

- Start a stream in a **fresh session**. Context resets to full immediately;
  waiting for a quota window to roll over is usually unnecessary, because
  context is the constraint that binds first and the one a new session clears.
- Assign **one stream per session**. Two is where it gets tight.
- **Do not have agents read `CLAUDE.md` whole.** It is over 500 lines and grows
  every time a decision is recorded. An agent reading it in full pays for the
  entire spec to learn about one stream. Point at sections.
- Size every task so it is **safe to abandon**. Small commits, each
  independently mergeable. No stream that only pays off if it runs to
  completion — because nobody can see the ceiling coming.

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

Streams 5 and 6 touch nothing in common and nothing the interactive session
owns — no `apps/`, no `packages/core/schemas.py`, no routers. Either can merge
whenever it is green.

Whoever is driving interactively takes the shared surface, because conflicts
there are the expensive kind and that context is already loaded.
