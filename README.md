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

When something is not working, open **`/setup`** on the dashboard: it names each
missing piece and the command that fixes it. Back up with `make backup` and
prove the backup with `make backup-verify` (docs/USAGE.md §12).

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
- **The API refuses non-local callers, and the dashboard binds loopback.** The
  API has no authentication and can submit real applications, so it rejects any
  client that is not loopback even if you start it with `--host 0.0.0.0`. That
  guard reads the socket peer, so it cannot be forged — but it also cannot see
  past a proxy: the dashboard rewrites `/api/*` to the API, which means a
  network-bound dashboard is a network-bound API however the guard is set. Both
  `make web` and `next start` therefore bind `127.0.0.1`. Override deliberately
  with `JOBRUNNER_WEB_HOST`, on a network you trust.
- **One worker per `WORKER_ID`.** Browser profiles are locked; a second worker
  sharing an id fails loudly instead of corrupting the session store.
- **Your résumé and application data are PII.** They stay on your machine in
  `storage/`, which is gitignored — nothing in there is ever committed.

## Running it

Requirements: Python 3.12, Docker (for Postgres + pgvector), Node.js (for the
dashboard), and two things pip cannot install — Pango for PDF rendering and a
Playwright browser. `make doctor` checks all of them and prints the fix for
whatever is missing; the list is under **First-run notes** below.

```bash
git clone <this-repo>
cd Job_Runner
cp .env.example .env        # fill in your own values — .env is gitignored

make install                # venv + dependencies
make up                     # Postgres + pgvector, creates jobrunner and jobrunner_test
make migrate                # apply the schema

.venv/bin/playwright install chromium   # the browser that fills forms
make nltk-data              # the POS tagger the fabrication guard needs
make doctor                 # confirms the above, and says what is still missing

make gate-0                 # lint, types, migration drift, full test suite

make api                    # http://127.0.0.1:8000
make worker                 # in a second terminal
make web-install            # once: installs the dashboard's npm dependencies
make web                    # the dashboard, http://127.0.0.1:3001
```

`make web-install` is a separate step and `make web` does not run it — a first
run without it fails on a missing `next` binary rather than on anything that
names the real problem.

`make nltk-data` is likewise not part of `make install`, because it is a
download rather than a package. Skipping it does not fail: the fabrication
guard falls back to matching on capitalization, which cannot see a lowercase
invented claim. `GuardReport.extractor` records which one ran and `make doctor`
reports it — a guard that quietly loses a check is worse than one that never
had it.

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

Run `make doctor` rather than working through these by hand — it checks each
one and prints the fix. They are listed because knowing *why* a step exists is
what tells you whether skipping it matters.

- **macOS needs Pango for PDF rendering.** WeasyPrint links against system
  libraries that macOS does not ship: `brew install pango cairo gdk-pixbuf
  libffi`. Without it, `import weasyprint` fails with a `libgobject` load error
  and résumé rendering will not work. On Debian or Ubuntu the equivalent is
  `apt-get install libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0`;
  most Linux desktops already have them. A missing Pango does not fail cleanly
  — it segfaults pytest partway through a run.
- **Install the browser once**: `.venv/bin/playwright install chromium`.
  Playwright pins its browser build to the wheel version, which is why
  `pyproject.toml` pins the wheel to a single minor.
- **The tagger data is a download**: `make nltk-data`. The fabrication guard
  (CLAUDE.md §2.1) POS-tags rewritten bullets to find noun-phrase claims;
  without the data it falls back to a capitalization heuristic that cannot see
  a lowercase fabrication. Nothing fails loudly, which is exactly why it is
  worth doing before you trust a tailored résumé.
- **The dashboard has its own dependencies**: `make web-install` before
  `make web`, once per checkout.
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

## AI code review with Open Code Review

[Alibaba Open Code Review](https://github.com/alibaba/open-code-review) is an
optional development tool for reviewing changes locally and posting findings
on GitHub pull requests. It is separate from Job Runner's résumé-tailoring
provider settings: configuring OCR does not change `LLM_PROVIDER` in `.env`.
The OpenRouter setup below sends reviewed code and relevant context to a remote
model; it is not an offline review.

### Setup status — September 19, 2026

- The OCR CLI was installed locally at version `1.12.6`.
- A connection test succeeded with OpenRouter and
  `deepseek/deepseek-v4-flash-0731:free`.
- A local review in `Job_Runner` found 52 changed files, selected 20, and
  excluded 32 using path/extension rules. That output confirms file selection,
  not completion of the review.
- The repository owner reported adding the `OPENROUTER_API_KEY` GitHub Actions
  secret and saving the PR-review workflow.
- At documentation update time, this local checkout did not contain
  `.github/workflows/ai-review.yml`. Confirm the committed GitHub version before
  adding another copy. A successful GitHub review run has not yet been verified.

An API key was exposed during setup. Revoke that key in
[OpenRouter settings](https://openrouter.ai/settings/keys) and use a replacement
in both local OCR configuration and the GitHub secret. Never put keys in this
README, workflow YAML, commits, screenshots, or chat messages.

### Local setup

Use an editor's integrated terminal or macOS Terminal, not a SQL query window.
OCR requires Git 2.41 or newer; the npm installation method also needs Node.js
and npm.

```bash
git --version
node --version
npm --version
npm install -g @alibaba-group/open-code-review@1.12.6
ocr version
```

Configure OpenRouter as a custom provider:

```bash
ocr config set provider review-openrouter
ocr config set custom_providers.review-openrouter.url https://openrouter.ai/api/v1
ocr config set custom_providers.review-openrouter.protocol openai
ocr config set custom_providers.review-openrouter.model deepseek/deepseek-v4-flash-0731:free
```

In **macOS zsh**, paste and run the following block together. When the prompt
appears, paste the replacement key once and press Enter. Input is hidden: no
characters or dots appear. The block saves the key before clearing the temporary
shell variable, then tests the connection.

```zsh
read -rs "ocrkey?Paste NEW OpenRouter key, then press Enter: " &&
printf '\n' &&
ocr config set custom_providers.review-openrouter.api_key "$ocrkey" &&
unset ocrkey &&
ocr llm test
```

The key is stored in OCR's user configuration, outside the repository. Local
configuration does not automatically populate GitHub Actions secrets.

From the repository root, review current changes:

```bash
ocr review --effort low
```

Or scan a specific existing file, replacing the example path:

```bash
ocr scan --path "path/to/file.py" --effort low
```

`low` uses one review round, but a review can still make multiple API calls.
Messages saying files were filtered are not errors. Check the final review
output for findings, failures, and skipped files. Local runs do not post to
GitHub automatically.

### GitHub pull-request reviews

In the GitHub repository, open **Settings → Secrets and variables → Actions →
New repository secret**. Name it `OPENROUTER_API_KEY` and use the replacement
OpenRouter key as its value.

Save the following as `.github/workflows/ai-review.yml`, preserving indentation.
Keep `${{ secrets.OPENROUTER_API_KEY }}` unchanged; GitHub resolves it at runtime.
The YAML file must not include Markdown code fences.

```yaml
name: AI Code Review

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  contents: read
  pull-requests: write

concurrency:
  group: ai-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  review:
    if: >-
      github.event.pull_request.draft == false &&
      github.event.pull_request.head.repo.full_name == github.repository &&
      github.actor != 'dependabot[bot]'
    runs-on: ubuntu-latest
    timeout-minutes: 30

    steps:
      - name: Review pull request
        uses: alibaba/open-code-review@7a571b78d3493b249f6ad14d835c6a79a0a67d2e # v1.12.6
        with:
          ocr_version: '1.12.6'
          llm_url: https://openrouter.ai/api/v1
          llm_auth_token: ${{ secrets.OPENROUTER_API_KEY }}
          llm_model: deepseek/deepseek-v4-flash-0731:free
          llm_use_anthropic: 'false'
          llm_extra_body: '{}'
          effort: low
          review_concurrency: '1'
          stream_progress: 'true'
          sticky_summary: 'true'
          incremental: 'true'
```

The action installs OCR, checks out the repository, reviews the PR diff, and
posts a summary and inline findings. It uses GitHub's automatically supplied
token; no personal access token is needed. The action commit and CLI version
are pinned separately for reproducibility.

To verify the setup:

1. Ensure the workflow is committed to the default branch (usually `main`).
2. Create a branch in the same repository and commit one small code change.
3. Open a non-draft PR against the default branch.
4. Open **Actions → AI Code Review** and inspect the job log.
5. Check the PR's conversation and changed files for review output.

New commits on that PR trigger another review. This configuration skips drafts,
fork PRs, and Dependabot PRs. Saving a secret or pushing directly to `main`
does not trigger it. The workflow runs on GitHub even when your Mac is offline.
`incremental` avoids overlapping inline comments; it does not by itself reduce
the code sent for review on later runs.

### Troubleshooting and usage limits

- **`quote>` in Terminal:** an opening quote is unmatched. Press Ctrl+C and
  rerun the complete command from a normal shell prompt.
- **Key is `(not set)`:** the key was not captured or was cleared before saving.
  Run the complete hidden-input block above; do not run `unset` separately first.
- **Invalid workflow YAML:** preserve spaces and remove pasted Markdown fences,
  HTML entities such as `&#x20;`, backslashes before underscores, and extra quotes.
- **401:** check that the key is valid and saved in the environment being used.
- **429:** inspect OpenRouter usage; free quota or upstream capacity may be
  exhausted. A PR review can consume many requests.
- **GitHub job skipped:** check whether the PR is a draft, comes from a fork,
  or was triggered by Dependabot.
- **GitHub cannot post comments:** check the run error and repository or
  organization Actions policy; the workflow requests `pull-requests: write`.

The `:free` model was working during setup, but hosted model availability,
quotas, and pricing can change. Check [OpenRouter activity](https://openrouter.ai/activity)
and [current limits](https://openrouter.ai/docs/api_reference/limits). GitHub
Actions runner usage is separate from model API usage. AI findings supplement
the project's tests and human review; they do not prove code is correct.

References: [OCR configuration](https://github.com/alibaba/open-code-review/blob/main/pages/src/content/docs/en/configuration.md),
[GitHub Actions integration](https://github.com/alibaba/open-code-review/tree/main/examples/github_actions),
and [GitHub Actions secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets).

## License

See [`LICENSE`](./LICENSE).
