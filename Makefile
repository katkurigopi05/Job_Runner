.PHONY: install up down migrate revision test lint fmt typecheck check \
        check-migrations api worker gate-0

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

api:
	$(PY)/uvicorn apps.api.main:app --reload --host 127.0.0.1 --port 8000

worker:
	$(PY)/python -m apps.worker.run

# Gate 0 — CLAUDE.md §9. All assertions covered:
#   pytest green, POST /applications reaches submitted, invalid transition
#   raises, duplicate (candidate_id, url) returns 409, ApplicationEvent rows
#   exist for every transition, ruff + mypy clean, migrations match models.
gate-0: lint typecheck check-migrations test
	@echo "gate-0 passed"
