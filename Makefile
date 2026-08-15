.PHONY: install up down migrate revision test lint fmt typecheck check \
        check-migrations api worker gate-0 gate-1 gate-1-live

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
	$(PY)/mypy packages/core packages/ats

# Schema drift: the migrations must fully describe the models.
check-migrations:
	$(PY)/alembic check

check: lint typecheck test

api:
	$(PY)/uvicorn apps.api.main:app --reload --host 127.0.0.1 --port 8000

worker:
	$(PY)/python -m apps.worker.run

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
	REQUIRE_DB=1 $(PY)/pytest -q tests/test_greenhouse.py
	@echo "gate-1 (offline) passed"

# make gate-1-live URL=https://boards.greenhouse.io/<company>/jobs/<id>
gate-1-live:
	@test -n "$(URL)" || (echo "set URL=<greenhouse posting url>" && exit 1)
	$(PY)/python -m scripts.live_check "$(URL)"
