"""The compose file starts Postgres with settings sized for this database.

The image's stock settings give Postgres 128 MB of shared buffers. On
2026-11-11 the database was 3.6 GB and nothing recorded which queries were
slow. `pg_stat_statements` only works when the server preloads it, so a
compose file that drops the `command` silently turns the measurements off.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_SETTINGS = (
    "shared_buffers",
    "effective_cache_size",
    "maintenance_work_mem",
    "work_mem",
    "shared_preload_libraries",
)


def _settings() -> dict[str, str]:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    command = compose["services"]["postgres"]["command"]
    assert command[0] == "postgres"
    flags, values = command[1::2], command[2::2]
    assert set(flags) == {"-c"}, "every setting is passed as `-c name=value`"
    return dict(value.split("=", 1) for value in values)


def test_postgres_is_given_every_named_setting() -> None:
    assert set(_settings()) == set(REQUIRED_SETTINGS)


def test_query_statistics_are_preloaded() -> None:
    assert "pg_stat_statements" in _settings()["shared_preload_libraries"].split(",")


def test_shared_memory_is_raised_for_parallel_queries() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["services"]["postgres"]["shm_size"] == "256mb"


def test_first_init_creates_the_statistics_extension() -> None:
    script = (ROOT / "scripts" / "init-db.sql").read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;" in script
