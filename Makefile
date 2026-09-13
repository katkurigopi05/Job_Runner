.PHONY: install up down migrate revision test lint fmt typecheck check \
        check-migrations api worker workers mcp web web-install validate-seeds discover rescore fit-topics import-portals \
        bench-matching export-labels import-csv inspect-csv registry-sync probe-bespoke import-mail score-mail review-resume load-golden validate-seeds-write vault-key gate-0 gate-1 gate-1-live gate-2 gate-2-live gate-3 gate-4 gate-5 gate-6 \
        gate-1-only gate-2-only gate-3-only gate-4-only gate-5-only gate-6-only

PY := .venv/bin

install:
	python3.12 -m venv .venv
	$(PY)/pip install --upgrade pip
	$(PY)/pip install -e ".[dev]"

up:
	docker compose up -d

down:
	docker compose down

migrate:
	$(PY)/alembic upgrade head

# make revision m="add lever adapter tables"
revision:
	$(PY)/alembic revision --autogenerate -m "$(m)"

test:
	$(PY)/pytest -q

lint:
	$(PY)/ruff check .
	$(PY)/ruff format --check .

fmt:
	$(PY)/ruff format .
	$(PY)/ruff check --fix .

typecheck:
	$(PY)/mypy packages/core packages/ats packages/github packages/tailor \
		packages/crawler packages/matching packages/inbox

# Schema drift: the migrations must fully describe the models.
check-migrations:
	$(PY)/alembic check

check: lint typecheck test

api:
	$(PY)/uvicorn apps.api.main:app --reload --host 127.0.0.1 --port 8000

worker:
	$(PY)/python -m apps.worker.run

# make workers n=4 — one process, n independent claimants. The queue was
# always safe for this (SKIP LOCKED); nothing could start more than one.
workers:
	$(PY)/python -m apps.worker.run --workers $(or $(n),4)

# Speaks MCP over stdio; Claude Code launches it itself via .mcp.json.
# Run it by hand only to check it starts. It needs `make api` running.
web-install:
	cd apps/web && npm install

# The dashboard. Talks to the API through a Next rewrite rather than from the
# browser, so the API keeps refusing non-loopback callers and needs no CORS.
web:
	cd apps/web && npm run dev

mcp:
	$(PY)/python -m apps.mcp.server

# Sequential live validation. A Greenhouse API 404 is checked against the
# rendered board before the slug is reported missing. Expect ~50 minutes.
tailor-batch:
	$(PY)/python -m scripts.batch_tailor

eval-tailor:
	$(PY)/python -m scripts.eval_tailor

doctor:
	$(PY)/python -m scripts.doctor

# A fresh Fernet key for VAULT_KEY. Printed, never written: .env is
# gitignored and stays that way, and appending blindly would leave two
# VAULT_KEY lines with the stale one winning on some readers.
vault-key:
	@$(PY)/python -c 'from packages.core.vault import generate_key; print(generate_key())'

validate-seeds:
	$(PY)/python -m packages.crawler.validate seeds/companies.yaml

# Same sweep, but records every verdict in the registry: `checked` and
# `state` per entry, and dead boards moved to `retired:` with the statuses
# that condemned them rather than deleted. Needs network egress.
validate-seeds-write:
	$(PY)/python -m packages.crawler.validate seeds/companies.yaml --write

# make worktree NAME=workable BRANCH=feat/workable-adapter
#
# A worktree for a second agent, set up so `make gate-0` actually runs there.
# Three things bite otherwise, and all three did:
#
#   - no .venv, so every $(PY)/ command fails
#   - no .env, because it is gitignored and does not travel with a worktree,
#     so alembic cannot reach the database
#   - a system python that is not the project's 3.12, so `python -m playwright`
#     reports missing when it is installed
#
# The symlink and the exported URLs fix all three. .env itself is deliberately
# not copied: one file, one place, and a second copy is a secret waiting to be
# committed from a directory nobody is watching.
worktree:
	@test -n "$(NAME)" || (echo "set NAME=<short-name>" && exit 1)
	@test -n "$(BRANCH)" || (echo "set BRANCH=<branch-name>" && exit 1)
	git worktree add /private/tmp/Job_Runner_$(NAME) -b $(BRANCH) origin/main
	ln -sfn $(CURDIR)/.venv /private/tmp/Job_Runner_$(NAME)/.venv
	@echo
	@echo "cd /private/tmp/Job_Runner_$(NAME)"
	@echo "Then export these before running make gate-0:"
	@grep -E '^(DATABASE_URL|TEST_DATABASE_URL|VAULT_KEY)=' .env 2>/dev/null \
		| sed 's/^/  export /' || echo "  (no .env found — copy from .env.example)"

# Gate 0 — CLAUDE.md §9. All assertions covered:
#   pytest green, POST /applications reaches submitted, invalid transition
#   raises, duplicate (candidate_id, url) returns 409, ApplicationEvent rows
#   exist for every transition, ruff + mypy clean, migrations match models.
#
# REQUIRE_DB=1 so a missing database fails the gate instead of skipping the
# tests that do most of the asserting.
# The test files each labelled gate runs. CI names these gates too, and used to
# repeat the file lists in `.github/workflows/ci.yml` — which drifted: the CI
# step called "gate-3" ran 131 tests where `make gate-3` ran 178, and "gate-5"
# ran 150 where the gate ran 243. Both still *ran* under gate-0, so nothing was
# unchecked; what was lost is the label saying which phase regressed, which is
# the exact cost §13 warns about. One definition, read by both.
GATE1_TESTS := tests/test_greenhouse.py tests/test_greenhouse_har.py \
  tests/test_lever.py tests/test_ashby.py tests/test_workable.py \
  tests/test_application_route.py tests/test_adapter_fill.py \
  tests/test_posting_readiness.py tests/test_answers_use_the_adapters_key_map.py
GATE2_TESTS := tests/test_gate2.py tests/test_worker.py
GATE3_TESTS := tests/test_no_fabrication.py tests/test_cover_letter.py \
  tests/test_apply_cover_letter.py tests/test_tailor_cache.py \
  tests/test_apply_uploads_tailored.py
GATE4_TESTS := tests/test_mcp.py
GATE5_TESTS := tests/test_crawler.py tests/test_matching.py tests/test_jsonld.py \
  tests/test_bespoke_probe.py tests/test_labeling_loop.py \
  tests/test_posting_url_is_reachable.py \
  tests/test_crawl_store.py tests/test_crawl_runs.py tests/test_crawl_dispatch.py \
  tests/test_crawl_scheduler.py tests/test_shared_ratelimit.py \
  tests/test_incremental_matching.py tests/test_workday.py \
  tests/test_migration_posting_dedupe.py \
  tests/test_registry_sync.py tests/test_worker_host_blocking.py \
  tests/test_matching_threshold.py tests/test_csv_to_match_e2e.py \
  tests/test_eligibility.py tests/test_matches_api.py \
  tests/test_feed_reads_as_encouraging.py
GATE6_TESTS := tests/test_inbox.py tests/test_inbox_duplicates.py \
  tests/test_tracker_columns.py

gate-0: lint typecheck check-migrations
	REQUIRE_DB=1 $(PY)/pytest -q
	@echo "gate-0 passed"

# Gate 1 — CLAUDE.md §9. The offline half: the adapter drives a real Chromium
# against a local fixture of a Greenhouse form, end to end.
#
# The other half (a LIVE Greenhouse posting) cannot run here and is not
# asserted by this target. Run `make gate-1-live URL=<posting>` for that.
gate-1: gate-0
	REQUIRE_DB=1 $(PY)/pytest -q $(GATE1_TESTS)
	@echo "gate-1 (offline) passed"

# Gate 2 — CLAUDE.md §9. The offline half: the review queue carries every
# unfilled field with the employer's original wording, and approving resumes
# the run. The fill-rate half of the gate needs a real posting and a real
# profile, which is `make gate-2-live`.
gate-2: gate-0
	REQUIRE_DB=1 $(PY)/pytest -q $(GATE2_TESTS)
	@echo "gate-2 (offline) passed"

# make gate-2-live URL=https://job-boards.greenhouse.io/<company>/jobs/<id>
#
# The half that cannot be faked: fills a real form from the real profile and
# reports what fraction of fields it answered with no manual input. §9 asks for
# >=80%.
gate-2-live:
	@test -n "$(URL)" || (echo "set URL=<greenhouse posting url>" && exit 1)
	$(PY)/python -m scripts.live_check "$(URL)"

# Gate 3 — CLAUDE.md §9 Phase 3. The fabrication merge gate: 20 job descriptions
# crossed with 3 resumes, plus the two deliverables the gate's own wording asks
# for and never checked. The cover letter and the per-company tailoring cache
# were both built, both tested, and neither ran here — so "gate-3 passes" meant
# less than "Phase 3 works", which is exactly the gap CLAUDE.md §15 exists to
# stop. test_apply_uploads_tailored is in for the same reason: it asserts the
# tailored file is the one that reaches the employer, which is the defect that
# survived a green Gate 3 for as long as nothing checked it.
#
# The fabrication half is unchanged: 20 job descriptions crossed with 3
# résumés, plus adversarial cases the guard must reject and legitimate
# rewrites it must allow, plus the tailored-PDF round trip.
gate-3: gate-0
	REQUIRE_DB=1 $(PY)/pytest -q $(GATE3_TESTS)
	@echo "gate-3 passed"

# Gate 5 — CLAUDE.md §9. A full cycle inside the rate limit, a second run
# emitting zero postings, and hand-labeled postings ranking sanely.
#
# The JSON-LD suites are in for the reason the tailored-résumé one is in
# gate-3: they assert wiring the module's own tests cannot see — that the
# crawler reaches a bespoke page at all, and that validation does not condemn
# every live one.
#
# test_labeling_loop is in because this gate's second half is about label
# quality, and the loop that produces owner-graded labels can fail in a way no
# count reveals: a corpus drawn only from the ranker's own shortlist carries
# the sampling bias `provenance: owner` is supposed to mean it escaped.
gate-5: gate-0
	REQUIRE_DB=1 $(PY)/pytest -q $(GATE5_TESTS)
	@echo "gate-5 passed"

# Gate 4 — CLAUDE.md §9. A full apply-to-review cycle driven by tool calls
# alone, plus the tool surface's own invariants.
gate-4: gate-0
	REQUIRE_DB=1 $(PY)/pytest -q $(GATE4_TESTS)
	@echo "gate-4 passed"

# Gate 6 — CLAUDE.md §9. Alias routing to the right application, and
# classification accuracy on 30 hand-labeled recruiter emails.
gate-6: gate-0
	REQUIRE_DB=1 $(PY)/pytest -q $(GATE6_TESTS)
	@echo "gate-6 passed"

# The labelled subsets on their own, without re-running gate-0. CI runs gate-0
# once and then these for the label; a developer wants `make gate-N`, which
# keeps the gate-0 dependency so a gate cannot pass on a red suite.
gate-1-only:
	REQUIRE_DB=1 $(PY)/pytest -q $(GATE1_TESTS)
gate-2-only:
	REQUIRE_DB=1 $(PY)/pytest -q $(GATE2_TESTS)
gate-3-only:
	REQUIRE_DB=1 $(PY)/pytest -q $(GATE3_TESTS)
gate-4-only:
	REQUIRE_DB=1 $(PY)/pytest -q $(GATE4_TESTS)
gate-5-only:
	REQUIRE_DB=1 $(PY)/pytest -q $(GATE5_TESTS)
gate-6-only:
	REQUIRE_DB=1 $(PY)/pytest -q $(GATE6_TESTS)

# make gate-1-live URL=https://boards.greenhouse.io/<company>/jobs/<id>
gate-1-live:
	@test -n "$(URL)" || (echo "set URL=<greenhouse posting url>" && exit 1)
	$(PY)/python -m scripts.live_check "$(URL)"

# make discover — one aggregator sweep, then promote what resolved into
# seeds/companies.yaml. Broad and slow; the crawl is narrow and fast.
discover:
	$(PY)/python -m scripts.discover

# make crawl — poll the company registry for new postings.
#
# Enqueues; `make worker` does the work. `apps/worker/crawl_job.py` has been
# wired into the worker since Phase 5 and nothing ever enqueued it, so the
# registry was polled only when someone inserted a row by hand — which is why
# postings went stale with nothing looking broken.
#
#     make crawl                        one cycle over the curated seed registry
#     make crawl force=1                re-emit unchanged postings (rarely wanted)
#     make crawl dispatch=1             sweep the companies *table* instead: one
#                                       task per company, discovery for those
#                                       with no verified board yet. This is the
#                                       only path that reaches an imported CSV.
#     make crawl dispatch=1 limit=20 once=1
#                                       a bounded sweep that does not re-arm
#                                       itself — what a pilot wants.
#
# The default is the seed-file cycle, unchanged. `dispatch=1` is opt-in because
# it polls a candidate table that can hold thousands of rows, and a command
# whose cost changes silently is worse than one more flag.
crawl:
	$(PY)/python -m scripts.crawl $(if $(force),--force,) $(if $(filter 1,$(dispatch)),--dispatch,) $(if $(limit),--limit $(limit),) $(if $(filter 1,$(once)),--once,)

# make rescore — re-score every open posting against the profiles as they are
# now. Crawling already re-scores, but returns early when the sweep emitted
# nothing, so a résumé change on its own never reaches the feed without this.
#   make rescore p=backend   one profile
#   make rescore dry=1       report the change, write nothing
#   make rescore re=1        re-encode every posting first — required after an
#                            EMBEDDING_BACKEND change, since stored vectors are
#                            otherwise reused and the two are not comparable
# Rank the scorer variants against seeds/labeled_matches.yaml. Prints the
# §47 experiment rows and a verdict that is usually "not established" — see
# docs/ML_EVALUATION.md for why that is the harness working.
# make review-resume r=path/to/resume.pdf j=path/to/jd.txt
# Scores a résumé against a posting and reports what to fix, from the
# deterministic scorers rather than a model's opinion.
# make load-golden — put the twelve crawled fixture postings into the database,
# so /matches, scoring and the review screen have real job-description prose to
# work on where the crawler cannot reach a board.
load-golden:
	$(PY)/python -m scripts.load_golden

review-resume:
	@test -n "$(r)" || (echo "set r=<résumé path>" && exit 1)
	$(PY)/python -m scripts.review_resume --resume "$(r)" $(if $(j),--posting-file "$(j)",) $(if $(g),--golden "$(g)",)

bench-matching:
	$(PY)/python -m scripts.bench_matching $(ARGS)

# Export the owner's swipe decisions as a labeled set. Gate 5 asks whether the
# ranker works on this owner's material and every label in the repo is a
# fixture; /swipe has been recording real judgements all along and nothing
# read them back out. See packages/matching/feedback.py for what a binary,
# feed-ordered label does and does not license.
export-labels:
	$(PY)/python -m scripts.export_labels $(if $(p),--profile $(p),) $(if $(out),--out $(out),) $(if $(kind),--kind $(kind),)

# Gate 6 asks for 30 hand-labeled *real* recruiter emails; inbound_messages is
# 0 and the fixtures were written beside the patterns that read them. Export
# your recruiter mail, label it, and score against it.
# Sort a CSV of companies + careers URLs into what we can crawl today. Offline,
# so 3,000 rows take seconds. The bespoke remainder is written out as the work
# queue for a generic extractor rather than counted and dropped.
#
# Three different writes, deliberately separate:
#   make import-csv src=...              report only, touches nothing
#   make import-csv src=... register=1   every row into the companies table,
#                                        so discovery and dispatch can reach
#                                        them. Idempotent; a board discovery
#                                        has already verified is not downgraded.
#   make import-csv src=... write=1      append the board-shaped rows to the
#                                        curated YAML registry
# `register` is the one that makes a sheet reachable by the crawler; `write` is
# the one that edits a git-tracked file. See packages/crawler/registry.py on
# which store owns which status.
import-csv:
	@test -n "$(src)" || (echo "set src=<companies.csv>" && exit 1)
	$(PY)/python -m scripts.import_companies "$(src)" $(if $(out),--out $(out),) $(if $(filter 1,$(register)),--register,) $(if $(filter 1,$(write)),--write,)

# What a sheet actually contains, before importing it. Offline, read-only, and
# reproducible: column roles, blank and duplicate rows, how each careers URL
# classifies by structure, and what read_rows/triage will do with it. Run this
# when a count in a report does not match what you expected — it recomputes
# rather than restating.
#   make inspect-csv src=companies.csv
inspect-csv:
	@test -n "$(src)" || (echo "set src=<companies.csv>" && exit 1)
	$(PY)/python -m scripts.inspect_csv "$(src)"

# Project the curated YAML registry into the companies table the dispatcher
# reads. Idempotent, cannot reactivate a retired board, and will not overwrite
# verification evidence newer than the seed file's own `checked` stamp.
#   make registry-sync
registry-sync:
	$(PY)/python -m scripts.registry_sync $(if $(seeds),--seeds $(seeds),)

# The other end of import-csv. Fetches each bespoke careers page once and asks
# whether it publishes schema.org JobPosting data; only the pages that answer
# become registry rows, because a page with none is a crawl cycle that parses
# nothing forever. Needs network egress, like validate-seeds.
#   make probe-bespoke n=50        sample first
#   make probe-bespoke write=1     promote the ones that publish
# `filter 1` rather than `$(if $(write),...)`: make reads any nonempty value as
# true, so `write=0` passed --write and mutated the registry. Strict, and it
# fails safe — an unrecognised value reports instead of writing, and the script
# prints "report only — pass write=1" so a typo is visible rather than silent.
probe-bespoke:
	$(PY)/python -m scripts.probe_bespoke $(if $(csv),--csv $(csv),) $(if $(n),-n $(n),) $(if $(filter 1,$(write)),--write,)

import-mail:
	@test -n "$(src)" || (echo "set src=<mbox|eml|dir>" && exit 1)
	$(PY)/python -m scripts.import_mail --src "$(src)" $(if $(out),--out $(out),)

score-mail:
	@test -n "$(ws)" || (echo "set ws=<worksheet.yaml>" && exit 1)
	$(PY)/python -m scripts.import_mail --score "$(ws)"

rescore:
	$(PY)/python -m scripts.rescore $(if $(p),--profile $(p),) $(if $(dry),--dry-run,) $(if $(re),--re-embed,)

# make fit-topics — fit LDA over the posting corpus and print the topics plus
# the entropy distribution. An analysis tool: it calibrates MAX_TOPIC_ENTROPY
# in legitimacy.py and persists nothing.
#   make fit-topics k=20 n=500
fit-topics:
	$(PY)/python -m scripts.fit_topics $(if $(k),-k $(k),) $(if $(n),-n $(n),)

# make import-portals f=../career-ops/templates/portals.example.yml
# Pulls their maintained company list into seeds/companies.yaml. Appends
# only; nothing you wrote by hand is touched. Add --dry-run to look first.
import-portals:
	@test -n "$(f)" || (echo "set f=<path to career-ops portals.yml>" && exit 1)
	$(PY)/python -m scripts.import_portals "$(f)" $(ARGS)

# The POS tagger data the fabrication guard needs. Separate from `install`
# because it is a download, not a package: pip cannot ship it. Without it the
# guard falls back to capitalization and says so on every GuardReport.
nltk-data:
	$(PY)/python -m scripts.fetch_nltk_data
