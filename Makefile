.PHONY: install up down migrate revision test lint fmt typecheck check \
        check-migrations api worker workers mcp web web-install validate-seeds discover \
        gate-0 gate-1 gate-1-live gate-2 gate-2-live gate-3 gate-4 gate-5 gate-6

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
validate-seeds:
	$(PY)/python -m packages.crawler.validate seeds/companies.yaml

# Gate 0 — CLAUDE.md §9. All assertions covered:
#   pytest green, POST /applications reaches submitted, invalid transition
#   raises, duplicate (candidate_id, url) returns 409, ApplicationEvent rows
#   exist for every transition, ruff + mypy clean, migrations match models.
#
# REQUIRE_DB=1 so a missing database fails the gate instead of skipping the
# tests that do most of the asserting.
gate-0: lint typecheck check-migrations
	REQUIRE_DB=1 $(PY)/pytest -q
	@echo "gate-0 passed"

# Gate 1 — CLAUDE.md §9. The offline half: the adapter drives a real Chromium
# against a local fixture of a Greenhouse form, end to end.
#
# The other half (a LIVE Greenhouse posting) cannot run here and is not
# asserted by this target. Run `make gate-1-live URL=<posting>` for that.
gate-1: gate-0
	REQUIRE_DB=1 $(PY)/pytest -q tests/test_greenhouse.py tests/test_greenhouse_har.py
	@echo "gate-1 (offline) passed"

# Gate 2 — CLAUDE.md §9. The offline half: the review queue carries every
# unfilled field with the employer's original wording, and approving resumes
# the run. The fill-rate half of the gate needs a real posting and a real
# profile, which is `make gate-2-live`.
gate-2: gate-0
	REQUIRE_DB=1 $(PY)/pytest -q tests/test_gate2.py tests/test_worker.py
	@echo "gate-2 (offline) passed"

# make gate-2-live URL=https://job-boards.greenhouse.io/<company>/jobs/<id>
#
# The half that cannot be faked: fills a real form from the real profile and
# reports what fraction of fields it answered with no manual input. §9 asks for
# >=80%.
gate-2-live:
	@test -n "$(URL)" || (echo "set URL=<greenhouse posting url>" && exit 1)
	$(PY)/python -m scripts.live_check "$(URL)"

# Gate 3 — CLAUDE.md §9. The fabrication merge gate: 20 job descriptions
# crossed with 3 résumés, plus adversarial cases the guard must reject and
# legitimate rewrites it must allow, plus the tailored-PDF round trip.
gate-3: gate-0
	REQUIRE_DB=1 $(PY)/pytest -q tests/test_no_fabrication.py
	@echo "gate-3 passed"

# Gate 5 — CLAUDE.md §9. A full cycle inside the rate limit, a second run
# emitting zero postings, and hand-labeled postings ranking sanely.
gate-5: gate-0
	REQUIRE_DB=1 $(PY)/pytest -q tests/test_crawler.py tests/test_matching.py
	@echo "gate-5 passed"

# Gate 4 — CLAUDE.md §9. A full apply-to-review cycle driven by tool calls
# alone, plus the tool surface's own invariants.
gate-4: gate-0
	REQUIRE_DB=1 $(PY)/pytest -q tests/test_mcp.py
	@echo "gate-4 passed"

# Gate 6 — CLAUDE.md §9. Alias routing to the right application, and
# classification accuracy on 30 hand-labeled recruiter emails.
gate-6: gate-0
	REQUIRE_DB=1 $(PY)/pytest -q tests/test_inbox.py
	@echo "gate-6 passed"

# make gate-1-live URL=https://boards.greenhouse.io/<company>/jobs/<id>
gate-1-live:
	@test -n "$(URL)" || (echo "set URL=<greenhouse posting url>" && exit 1)
	$(PY)/python -m scripts.live_check "$(URL)"

# make discover — one aggregator sweep, then promote what resolved into
# seeds/companies.yaml. Broad and slow; the crawl is narrow and fast.
discover:
	$(PY)/python -m scripts.discover
