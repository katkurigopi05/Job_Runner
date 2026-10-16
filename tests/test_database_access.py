"""Privileged database commands use a credential the runtime never loads."""

from __future__ import annotations

from pathlib import Path

import pytest

RUNTIME_URL = "postgresql://jobrunner_app:runtime-secret@localhost:5433/jobrunner"
MAINTENANCE_URL = "postgresql://jobrunner_admin:admin-secret@localhost:5433/jobrunner"


def test_maintenance_url_is_an_explicit_process_only_override(monkeypatch) -> None:
    from packages.core.database_access import maintenance_database_url

    monkeypatch.setenv("MAINTENANCE_DATABASE_URL", MAINTENANCE_URL)

    assert maintenance_database_url(RUNTIME_URL) == MAINTENANCE_URL


def test_privileged_commands_remain_backward_compatible(monkeypatch) -> None:
    from packages.core.database_access import maintenance_database_url

    monkeypatch.delenv("MAINTENANCE_DATABASE_URL", raising=False)

    assert maintenance_database_url(RUNTIME_URL) == RUNTIME_URL


def test_maintenance_secret_is_not_an_application_setting(monkeypatch) -> None:
    from packages.core.config import Settings

    monkeypatch.setenv("MAINTENANCE_DATABASE_URL", MAINTENANCE_URL)

    settings = Settings(database_url=RUNTIME_URL)

    assert "maintenance_database_url" not in Settings.model_fields
    assert MAINTENANCE_URL not in settings.model_dump().values()


def test_async_url_normalization_preserves_the_credential() -> None:
    from packages.core.database_access import async_database_url

    assert async_database_url(MAINTENANCE_URL) == MAINTENANCE_URL.replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )


def test_empty_maintenance_override_is_refused(monkeypatch) -> None:
    from packages.core.database_access import maintenance_database_url

    monkeypatch.setenv("MAINTENANCE_DATABASE_URL", "  ")

    with pytest.raises(ValueError, match="MAINTENANCE_DATABASE_URL is empty"):
        maintenance_database_url(RUNTIME_URL)


def test_maintenance_override_cannot_redirect_a_privileged_command(monkeypatch) -> None:
    from packages.core.database_access import maintenance_database_url

    monkeypatch.setenv(
        "MAINTENANCE_DATABASE_URL",
        "postgresql://jobrunner_admin:admin-secret@localhost:5433/another_database",
    )

    with pytest.raises(ValueError, match="must target the DATABASE_URL database"):
        maintenance_database_url(RUNTIME_URL)


def test_role_provisioning_requires_an_explicit_maintenance_url(monkeypatch) -> None:
    from packages.core.database_access import required_maintenance_database_url

    monkeypatch.delenv("MAINTENANCE_DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="must be set"):
        required_maintenance_database_url()


def test_runtime_modules_do_not_read_the_maintenance_credential() -> None:
    root = Path(__file__).resolve().parent.parent
    runtime_files = (
        "apps/api/main.py",
        "apps/worker/run.py",
        "packages/core/db.py",
    )

    for relative in runtime_files:
        source = (root / relative).read_text(encoding="utf-8")
        assert "MAINTENANCE_DATABASE_URL" not in source


def test_only_privileged_entrypoints_resolve_the_maintenance_credential() -> None:
    root = Path(__file__).resolve().parent.parent
    expected = {
        "migrations/env.py",
        "scripts/backup.py",
        "scripts/database_roles.py",
        "scripts/prune_postings.py",
    }
    readers = {
        str(path.relative_to(root))
        for parent in (root / "apps", root / "packages", root / "scripts", root / "migrations")
        for path in parent.rglob("*.py")
        if "maintenance_database_url" in path.read_text(encoding="utf-8")
        and path.name != "database_access.py"
    }

    assert readers == expected
