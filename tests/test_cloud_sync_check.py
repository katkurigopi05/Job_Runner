"""Résumés, browser sessions, backups and the vault key stay off cloud sync.

§2.8 keeps résumés on this machine, and §2.7 keeps `.env` and the vault key
out of the repository. The 2026-10-14 audit found the whole checkout in
`~/Desktop`, a folder iCloud Drive manages: `storage/` (résumés, filled-form
screenshots, the ATS browser sessions), `backups/`, `.secrets/` and `.env` all
inside it, and iCloud's conflict copies of tracked files dated the day before.

Moving them is the owner's decision, so this reports rather than moves:
`make doctor` and `/setup` say which of the four sit inside a synced folder,
and what to do. It is not blocking. The app works either way, and a check
that stops a crawl over where the checkout lives would be turned off.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from packages.core import doctor
from packages.core.config import get_settings
from packages.core.doctor import Health, check_cloud_sync, synced_folder


def test_a_path_inside_a_managed_folder_names_that_folder(tmp_path: Path) -> None:
    desktop = tmp_path / "Desktop"

    found = synced_folder(
        desktop / "Job_Runner" / "storage", managed=lambda folder: folder == desktop, home=tmp_path
    )

    assert found == desktop


def test_a_path_outside_every_managed_folder_is_none(tmp_path: Path) -> None:
    assert (
        synced_folder(tmp_path / "Code" / "x", managed=lambda folder: False, home=tmp_path) is None
    )


@pytest.mark.parametrize("client", ["Library/CloudStorage/Dropbox", "Library/Mobile Documents"])
def test_a_sync_clients_own_folder_counts_without_a_mark(tmp_path: Path, client: str) -> None:
    """Dropbox, Google Drive and OneDrive live under CloudStorage, iCloud
    Drive's own files under Mobile Documents. Neither needs the attribute."""
    found = synced_folder(
        tmp_path / client / "Job_Runner" / ".secrets", managed=lambda folder: False, home=tmp_path
    )

    assert found is not None
    assert str(found).startswith(str(tmp_path / "Library"))


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name in ("STORAGE_ROOT", "VAULT_ROOT", "BACKUP_ROOT"):
        monkeypatch.setenv(name, str(tmp_path / "Desktop" / "jr" / name.lower()))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_private_files_in_a_synced_folder_are_reported(roots: Path) -> None:
    desktop = roots / "Desktop"

    check = check_cloud_sync(
        managed=lambda folder: folder == desktop,
        home=roots,
        env_file=desktop / "jr" / ".env",
    )

    assert check.health is Health.FAIL
    assert check.required is False
    for name in ("storage", "vault", "backups", ".env"):
        assert name in check.detail
    assert str(desktop) in check.detail
    assert "STORAGE_ROOT" in check.fix


def test_private_files_outside_every_synced_folder_pass(roots: Path) -> None:
    check = check_cloud_sync(
        managed=lambda folder: False, home=roots, env_file=roots / "Code" / ".env"
    )

    assert check.health is Health.OK


def test_the_real_marker_is_read_without_raising_for_a_missing_path(tmp_path: Path) -> None:
    assert doctor._managed_by_file_provider(tmp_path / "does" / "not" / "exist") is False


def test_the_doctor_runs_it() -> None:
    assert "check_cloud_sync()" in inspect.getsource(doctor.run)
