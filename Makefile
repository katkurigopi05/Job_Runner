.PHONY: install up down migrate revision test lint fmt typecheck check gate-0

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
	$(PY)/mypy packages/core

# Schema drift: the migrations must fully describe the models.
check-migrations:
	$(PY)/alembic check

check: lint typecheck test

# Gate 0 — CLAUDE.md §9.
# Covers: pytest green, invalid transition raises, ApplicationEvent per
# transition, ruff + mypy clean, migrations match models.
# Still to add with the API routes: POST /applications reaching submitted,
# and duplicate (candidate_id, url) returning 409.
gate-0: lint typecheck check-migrations test
	@echo "gate-0 checks passed"
