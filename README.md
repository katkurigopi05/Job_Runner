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
   diff you can review. Tailoring only rephrases and reorders facts already in
   your source résumé — it never invents a skill, employer, date, or metric.
3. **Apply** — drives the real ATS form (Greenhouse, Lever, Ashby, Workable),
   fills every field it can map to your profile, and **parks unanswerable
   questions for you to answer** rather than guessing.
4. **Track** — ingests recruiter replies over email and routes them back to
   the right application.

See [`CLAUDE.md`](./CLAUDE.md) for the full build spec and non-negotiable
rules, and [`docs/TSENTA_ARCHITECTURE.md`](./docs/TSENTA_ARCHITECTURE.md) for
the architecture teardown this project is modeled on.

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
```

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

This project is built in phases (skeleton → first ATS → tailoring → MCP →
discovery → tracker), each gated by its own test suite. See `CLAUDE.md` §9 for
the current phase and what's implemented so far.

## License

See [`LICENSE`](./LICENSE).
