"""Database URLs for runtime and explicitly privileged commands.

The API and workers only use ``Settings.database_url``.  Alembic and restore
verification may opt into ``MAINTENANCE_DATABASE_URL`` from their process
environment, so the elevated credential can live in a separately sourced file
instead of the application's ``.env``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

MAINTENANCE_DATABASE_URL = "MAINTENANCE_DATABASE_URL"
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


def async_database_url(url: str) -> str:
    """Normalize a PostgreSQL URL to SQLAlchemy's asyncpg driver."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


def maintenance_database_url(runtime_url: str, environment: Mapping[str, str] | None = None) -> str:
    """Return the command-scoped elevated URL, or the legacy runtime URL.

    Falling back keeps existing installations working during the expansion
    step.  Once separate roles are provisioned, privileged commands receive
    the maintenance URL only in their own process environment.
    """
    values = os.environ if environment is None else environment
    if MAINTENANCE_DATABASE_URL not in values:
        return runtime_url
    maintenance_url = values[MAINTENANCE_DATABASE_URL].strip()
    if not maintenance_url:
        raise ValueError(f"{MAINTENANCE_DATABASE_URL} is empty")
    try:
        runtime = make_url(runtime_url)
        maintenance = make_url(maintenance_url)
    except ArgumentError:
        raise ValueError("database URLs must be valid") from None

    def location(url: URL) -> tuple[str, int, str | None]:
        host = (url.host or "localhost").strip("[]").casefold()
        return ("localhost" if host in _LOOPBACK else host, url.port or 5432, url.database)

    if location(runtime) != location(maintenance):
        raise ValueError(f"{MAINTENANCE_DATABASE_URL} must target the DATABASE_URL database")
    return maintenance_url


def required_maintenance_database_url(environment: Mapping[str, str] | None = None) -> str:
    """Return the elevated URL for role changes, refusing an implicit fallback."""
    values = os.environ if environment is None else environment
    maintenance_url = values.get(MAINTENANCE_DATABASE_URL, "").strip()
    if not maintenance_url:
        raise ValueError(f"{MAINTENANCE_DATABASE_URL} must be set for database role changes")
    return maintenance_url


__all__ = [
    "MAINTENANCE_DATABASE_URL",
    "async_database_url",
    "maintenance_database_url",
    "required_maintenance_database_url",
]
