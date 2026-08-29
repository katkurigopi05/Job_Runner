# Job Runner

A local, single-user job-application agent. It watches a curated list of company
career pages, scores new postings against your profile, tailors your résumé per
posting, and fills out the real ATS application form in a headless browser.

**Nothing is ever submitted without your explicit approval.** Every application
stops at a review screen — showing the filled form and a screenshot — and waits
for you to approve, edit, or reject it. Auto-submit exists as an opt-in setting
per profile, above a match-score threshold you choose; the shipped default is
off.

**Captcha solving and bot-detection evasion are out of scope.** When a site
blocks automation, the application is marked `manual_completion_required` and
you finish it by hand. This project does not attempt to defeat anti-bot
defenses, rotate residential proxies, or spoof browser fingerprints — that's a
hard boundary, not a missing feature.

If those two things aren't what you're looking for, this isn't the right tool.

## What it does

1. **Find** — polls a hand-picked list of career pages (not a mass crawl),
   detects new postings, and scores them against your profile.
2. **Prep** — rewrites your résumé bullets for the posting and generates a
   diff you can review. Tailoring rephrases and reorders facts already in your
   source résumé; the Projects section may add details GitHub reports for that
   repository, kept attributed to it. It never invents a skill, employer, date,
   or metric.
3. **Apply** — drives the real ATS form (Greenhouse, Lever, Ashby, Workable),
   fills every field it can map to your profile, and **parks unanswerable
   questions for you to answer** rather than guessing.
4. **Track** — ingests recruiter replies over email and routes them back to
   the right application.

See [`CLAUDE.md`](./CLAUDE.md) for the full build spec and non-negotiable
rules, and [`docs/TSENTA_ARCHITECTURE.md`](./docs/TSENTA_ARCHITECTURE.md) for
the architecture teardown this project is modeled on.

For a start-to-finish walkthrough, dashboard route reference, MCP command
examples, and troubleshooting, see [`docs/USAGE.md`](./docs/USAGE.md).

| Doc | What it covers |
|---|---|
| [`CLAUDE.md`](./CLAUDE.md) | build spec, the non-negotiable rules, and a record of what each gate does *not* prove |
| [`docs/USAGE.md`](./docs/USAGE.md) | walkthrough, routes, troubleshooting |
| [`docs/ML_EVALUATION.md`](./docs/ML_EVALUATION.md) | what the ranking numbers may and may not claim |
| [`docs/BACKLOG.md`](./docs/BACKLOG.md) | gap register against the job-discovery spec |
| [`docs/PARITY.md`](./docs/PARITY.md) | capability map against career-ops |
| [`docs/REFERENCE.md`](./docs/REFERENCE.md) | what the teardown implies for this build |

## How it knows any of this works

Three scores, kept apart on purpose. They answer different questions and fail
independently, so averaging them into one number hides which one broke.

| Score | Question | Where |
|---|---|---|
| **ATS parse** | can a machine turn this document into fields? | `packages/tailor/ats.py` |
| **ATS keywords** | does the résumé back the vocabulary this posting asks for? | `packages/tailor/ats.py` |
| **Recruiter** | would a person shortlist it? | `packages/tailor/recruiter.py` |

The recruiter score reads in four passes — a ten-second scan, a thirty-second
qualification check, a hiring-manager credibility pass, and a technical one.
It is deterministic: no model judges it, because a judge that drifts makes a
score that moves tell you nothing about which side moved.

It exists because the first two can be gamed and the third cannot be, by the
same move. On a keyword-stuffed résumé measured against a real crawled
posting, ATS keyword coverage rises from 0.18 to 0.46 while the recruiter
score falls from 0.640 to 0.403 and the verdict goes from *maybe* to *no*. A
test asserts that disagreement; if the two ever move together on that pair,
the second referee has stopped doing its job.

Nothing scores itself. The evaluators take a résumé and a posting, with no
parameter that could say "this is the optimized one" — a rewriter that could
grade its own output would certify itself.

### The ranking side

```bash
make bench-matching                          # the shipped scorer vs its ablations
make bench-matching ARGS="--tag adjacent"    # only the hard cases
```

Reports NDCG@K, MAP, MRR, precision and recall with bootstrap confidence
intervals, against a constant control that returns the same number for
everything. Anything that cannot beat that control by more than the interval
has not been shown to rank.

It usually declines to name a winner, and that is the harness working rather
than failing — the labels in `seeds/labeled_matches.yaml` are fixture-grade,
so no run over them may report a production candidate however good the numbers
look. [`docs/ML_EVALUATION.md`](./docs/ML_EVALUATION.md) records what the
numbers may and may not claim, and what data would have to arrive first.

[`docs/BACKLOG.md`](./docs/BACKLOG.md) is the gap register: every capability
the spec asks for, checked against the code, sized as buildable projects.

## Scope boundaries

- **Single user, local-first.** Runs on `localhost`. No multi-tenancy, no
  billing, no hosted deployment.
- **Work authorization and employment-history answers are copied verbatim**
  from your profile — never LLM-generated. These have legal consequences.
- **Crawling respects `robots.txt` and rate limits** (minimum 60s between
  requests to the same host). Apply only to postings you personally intend to
  pursue — this is not a spray-and-pray tool.
- **Secrets never touch the database in plaintext or logs.** ATS account
  passwords go through an encrypted vault, stored outside `storage/` so they
  never travel with your résumés and screenshots.
- **The API refuses non-local callers.** It has no authentication and can
  submit real applications, so it rejects anything that is not loopback even
  if you start it with `--host 0.0.0.0`.
- **One worker per `WORKER_ID`.** Browser profiles are locked; a second worker
  sharing an id fails loudly instead of corrupting the session store.
- **Your résumé and application data are PII.** They stay on your machine in
  `storage/`, which is gitignored — nothing in there is ever committed.

## Running it

Requirements: Python 3.12, Docker (for Postgres + pgvector), Node.js (for the
dashboard).

```bash
git clone <this-repo>
cd Job_Runner
cp .env.example .env        # fill in your own values — .env is gitignored

make install                # venv + dependencies
make up                     # Postgres + pgvector, creates jobrunner and jobrunner_test
make migrate                # apply the schema
make gate-0                 # lint, types, migration drift, full test suite

make api                    # http://127.0.0.1:8000
make worker                 # in a second terminal
make web                    # the dashboard, http://localhost:3001
```

The shipped default costs nothing and sends nothing anywhere: `LLM_PROVIDER=stub`
for tests, Ollama for anything local. Remote providers — Gemini, Anthropic,
OpenRouter, and Ollama's own hosted models (`ollama_cloud`, e.g.
`glm-5.3-flash:cloud`) — answer only when you name them, never because a key
is present in `.env`. Ollama's hosted models are a separate provider name
rather than a `:cloud` tag on `OLLAMA_MODEL`, because they are served over the
same `localhost:11434` API as the local ones and nothing else in the request
would tell you which you got.

`make gate-0` requires a running database and fails if it cannot reach one.
Bare `pytest` skips the database tests instead, so a fresh checkout is green
before `make up`.

### First-run notes

- **macOS needs Pango for PDF rendering.** WeasyPrint links against system
  libraries that macOS does not ship: `brew install pango`. Without it,
  `import weasyprint` fails with a `libgobject` load error and résumé
  rendering will not work. Linux users generally already have these.
- **Install the browser once**: `.venv/bin/playwright install chromium`.
  Playwright pins its browser build to the wheel version, which is why
  `pyproject.toml` pins the wheel to a single minor.
- **Port 5432** maps straight through. If you already run Postgres locally
  (Homebrew, Postgres.app), change the host side of the port mapping in
  `docker-compose.yml` or stop the other server first.
- **Python 3.12** — `make install` calls `python3.12` explicitly.

## Driving it from Claude Code

`.mcp.json` is committed, so Claude Code picks the server up when you open the
repo. Start the API first — the tools call it rather than the database, so the
approval gate and completeness checks have exactly one implementation:

```bash
make api        # the MCP tools talk to this
```

Then ask for what you want in plain language: *"is this URL supported?"*,
*"apply to this posting"*, *"what's in the review queue?"*, *"answer the
questions and approve it"*.

One deliberate absence in the tool surface: there is no tool that submits an
application. Approval releases it and the worker does the rest, so the gate
has exactly one implementation and no tool call can step around it.

Tailoring is built, but there is no single `tailor_resume` tool either.
Rewriting happens inside the apply pipeline, behind the fabrication guard;
what the tool surface exposes is the reviewing of it — `preview_resume`,
`inspect_application_resume`, `compare_tailoring` (the same posting through
two models, side by side), `select_tailoring`, and `edit_application_resume`.
An edit arriving over MCP is written by a model, so unlike an edit typed on
the review screen it is guard-checked, and the tool has no parameter that
could turn that off.

This project is built in phases (skeleton → first ATS → tailoring → MCP →
discovery → tracker), each gated by its own test suite. See `CLAUDE.md` §9 for
the current phase and what's implemented so far.

## License

See [`LICENSE`](./LICENSE).
