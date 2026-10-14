# Notes on CI, the Dockerfile and pinned dependencies

Moved here from the root `CLAUDE.md` on 2026-10-11, word for word, so they
load only when work touches this directory. "§N" below is a section of
the root file, which keeps every heading and says where each section went.

## §13: CI

`.github/workflows/ci.yml` runs on every push to `main`, every pull request,
weekly, and on demand. Two jobs, and they do not both run on every trigger —
see `image` below.

**`gates`** — the same checks `make gate-N` runs locally, on a machine that has
nothing installed. It brings up `pgvector/pgvector:pg16` as a service container,
installs the native libraries WeasyPrint loads through cffi, installs Playwright
Chromium, applies migrations, then runs `make gate-0` followed by every other
gate by name.

Gate 0 already runs the whole suite, so the named gates are subsets re-run for
labelling. That is deliberate: when CI goes red you want to be told which gate
broke, not that "a test failed".

**A new gate has to be added here too.** The named steps are a hand-maintained
list, so a `gate-N` that exists in the Makefile but not in `ci.yml` still gets
*run* — gate 0 covers every test — but a failure in it reports as a gate-0
failure and you lose the label that says which phase regressed.

That is the half this paragraph anticipated. The half that actually happened is
harder to see, because the CI step is right there wearing the correct name: the
steps repeated the *file lists* rather than calling `make gate-N`, and the two
copies drifted.

```text
gate-2   CI ran   4 tests, make gate-2 ran  18
gate-3   CI ran 131 tests, make gate-3 ran 178
gate-5   CI ran 150 tests, make gate-5 ran 243
```

Every one of those tests still ran under gate-0, so nothing went unchecked.
What was lost is the thing the named steps exist for: "gate-3 is green" in CI
had stopped meaning what `make gate-3` means, and the cover letter, the
tailoring cache and the tailored-résumé upload — the three suites §15 records
adding to gate-3 precisely so a green gate would stop over-claiming — were
never in the labelled step at all.

The lists now live once, as `GATEn_TESTS`, and CI runs `make gate-N-only` —
the same subset without the `gate-0` dependency, since CI has already run it
and a developer typing `make gate-3` should still get the full suite first.
`tests/test_ci_runs_every_gate.py` holds both properties: every gate is named
in CI, and no CI step spells out its own file list again.

**`image`** — builds the `Dockerfile`, imports every entry point inside it, and
checks Chromium is present. Nothing is pushed anywhere.

It does **not** run on pull requests. The repo is private, so Actions minutes
are metered on the free plan, and this job is the expensive half of a run — it
downloads Chromium a second time to build the image (~3 of the ~7 billable
minutes). What it proves changes when the `Dockerfile` or the dependency set
changes, not on every branch push, so it runs on `main`, weekly, and on
demand. **A pull request that touches either should be checked by hand with
`workflow_dispatch` before merging** — that is the cost of this trade, and it
is on you to remember it.

### Pinned dependencies

CI, the image and `make install` install from `constraints.txt`, an exact lock
of every base and dev dependency. Until 2026-09-30 they installed whatever was
newest, and on 2026-09-28 that turned `main` red with no code change:
SQLAlchemy 2.1.1 changed how `Select` is typed, mypy failed the gate, and every
open pull request inherited the failure. A PR's checks should describe the PR,
not the day it ran.

Two things keep the lock from going stale unnoticed:

- **The weekly and on-demand runs add a second gates leg** that installs the
  newest versions. When it fails, an upstream release has broken something.
  It fails a scheduled run, not somebody's pull request, and the lock still
  holds `main` green. When it passes, `make lock` (needs `uv`) refreshes the
  pins, and that PR's CI tests exactly what it pins.
- **`tests/test_dependencies_are_locked.py`** fails when a dependency in
  `pyproject.toml` has no pin, or a pin falls outside its range. Those are the
  two ways the lock stops agreeing with what it locks without anything else
  noticing.

SQLAlchemy is also capped below 2.1 in `pyproject.toml`. The suite passes under
2.1.1, so lifting the cap means fixing three annotations, as a change of its
own.

### Why this exists

Every dependency defect this project has had was invisible locally and only
appeared on a clean install: `email-validator`, `python-docx`,
`python-multipart`, `mcp`, `pyyaml`. `python-multipart` is the instructive one —
nothing imports it, FastAPI reaches for it at runtime, so no amount of reading
the source finds it. Only installing from nothing does.

The native side is the same story. WeasyPrint needs Pango and Playwright needs a
browser; neither is a Python dependency, and a missing Pango does not fail
cleanly — it segfaults pytest partway through the run.

### The Dockerfile is not a deployment

There is no registry, no orchestrator, and no hosted runtime. §1 still holds:
this runs on localhost for one user. The image exists so "it installs from
nothing" is checked on every commit.

Two things in it are load-bearing and easy to undo by accident:

- **No `chown -R` over `/opt/playwright`.** Changing a file's mode rewrites it
  into a new layer, so recursing over a 1.3GB browser added 947MB of duplicates.
  Chromium only needs to be readable and executable. Root can own it.
- **`.dockerignore` excludes `.env`, `.secrets/`, and `storage/`.** Those are
  the vault key, the encrypted credentials, and résumé PII. An image layer is
  forever, so keep them out of the build context.
