"""The database role command is dry-run first and redacts both credentials."""

from __future__ import annotations

from sqlalchemy.engine import make_url

from tests.conftest import TEST_DATABASE_URL


def test_database_role_command_dry_run_prints_no_password(monkeypatch, capsys) -> None:
    from packages.core.config import get_settings
    from scripts.database_roles import main

    maintenance = make_url(TEST_DATABASE_URL).set(password="admin-preview-secret")
    runtime = maintenance.set(username="jobrunner_app_preview", password="runtime-preview-secret")
    monkeypatch.setenv("DATABASE_URL", runtime.render_as_string(False))
    monkeypatch.setenv("MAINTENANCE_DATABASE_URL", maintenance.render_as_string(False))
    get_settings.cache_clear()
    try:
        assert main([]) == 0
    finally:
        get_settings.cache_clear()

    output = capsys.readouterr().out
    assert "DRY RUN: no database changes made" in output
    assert "runtime-preview-secret" not in output
    assert "admin-preview-secret" not in output
