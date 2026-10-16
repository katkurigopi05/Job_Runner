"""Private runtime files move by verified copy before configuration switches."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from packages.core.config import SettingsEnvFileError, get_settings, settings_env_file
from packages.core.private_paths import (
    MANIFEST_FILE,
    RUNTIME_ENV_FILE,
    PrivatePathError,
    migrate_private_paths,
    plan_private_paths,
    verify_private_paths,
)
from scripts.private_paths import main


@pytest.fixture
def private_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    source = tmp_path / "Desktop" / "Job_Runner"
    storage = source / "storage"
    vault = source / ".secrets"
    backups = source / "backups"
    (storage / "browser").mkdir(parents=True)
    vault.mkdir()
    backups.mkdir()
    (storage / "resume.pdf").write_bytes(b"resume")
    (storage / "browser" / ".profile-lock").write_text("retained")
    (storage / "browser" / "current-lock").symlink_to(".profile-lock")
    (vault / "ats.enc").write_bytes(b"ciphertext")
    (backups / "jobrunner.dump").write_bytes(b"database")

    env_file = source / ".env"
    env_file.write_text(
        "PRIVATE_SENTINEL=must-never-be-printed\n"
        f"STORAGE_ROOT={storage}\n"
        f"VAULT_ROOT={vault}\n"
        f"BACKUP_ROOT={backups}\n"
    )
    env_file.chmod(0o644)
    monkeypatch.setenv("JOBRUNNER_ENV_FILE", str(env_file))
    for name in ("STORAGE_ROOT", "VAULT_ROOT", "BACKUP_ROOT"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield {
        "source": source,
        "storage": storage,
        "vault": vault,
        "backups": backups,
        "env": env_file,
    }
    get_settings.cache_clear()


def test_an_explicit_external_env_file_is_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "runtime.env"
    env_file.write_text("STORAGE_ROOT=/private/from-file\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JOBRUNNER_ENV_FILE", str(env_file))
    get_settings.cache_clear()
    try:
        assert settings_env_file() == env_file
        assert get_settings().storage_root == "/private/from-file"
        monkeypatch.setenv("STORAGE_ROOT", "/private/from-process")
        get_settings.cache_clear()
        assert get_settings().storage_root == "/private/from-process"
    finally:
        get_settings.cache_clear()


def test_a_missing_explicit_env_file_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOBRUNNER_ENV_FILE", str(tmp_path / "missing.env"))
    get_settings.cache_clear()
    try:
        with pytest.raises(SettingsEnvFileError, match="JOBRUNNER_ENV_FILE"):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_the_default_env_file_remains_backward_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JOBRUNNER_ENV_FILE", raising=False)
    (tmp_path / ".env").write_text("STORAGE_ROOT=./old-location\n")
    get_settings.cache_clear()
    try:
        assert settings_env_file() == tmp_path / ".env"
        assert get_settings().storage_root == "./old-location"
    finally:
        get_settings.cache_clear()


def test_private_path_dry_run_inventories_without_writing(
    private_sources: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "Private" / "jobrunner"

    plan = plan_private_paths(destination, managed=lambda _path: False, home=tmp_path)
    assert plan.files == 5
    assert plan.bytes > 0
    assert main(["--destination", str(destination)]) == 0

    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "must-never-be-printed" not in output
    assert not destination.exists()
    assert private_sources["env"].read_text().startswith("PRIVATE_SENTINEL=")


def test_apply_copies_verifies_and_rewrites_only_the_copy(
    private_sources: dict[str, Path], tmp_path: Path
) -> None:
    destination = tmp_path / "Private" / "jobrunner"
    original_env = private_sources["env"].read_bytes()

    result = migrate_private_paths(destination, managed=lambda _path: False, home=tmp_path)

    assert result.destination == destination
    assert result.already_complete is False
    assert (destination / "storage" / "resume.pdf").read_bytes() == b"resume"
    assert (destination / "storage" / "browser" / ".profile-lock").read_text() == "retained"
    assert (destination / "storage" / "browser" / "current-lock").readlink() == Path(
        ".profile-lock"
    )
    assert (destination / "vault" / "ats.enc").read_bytes() == b"ciphertext"
    assert (destination / "backups" / "jobrunner.dump").read_bytes() == b"database"
    assert (destination / MANIFEST_FILE).is_file()

    runtime_env = destination / RUNTIME_ENV_FILE
    text = runtime_env.read_text()
    assert "PRIVATE_SENTINEL=must-never-be-printed" in text
    assert f"STORAGE_ROOT={destination / 'storage'}" in text
    assert f"VAULT_ROOT={destination / 'vault'}" in text
    assert f"BACKUP_ROOT={destination / 'backups'}" in text
    assert private_sources["env"].read_bytes() == original_env
    assert stat.S_IMODE(private_sources["env"].stat().st_mode) == 0o644

    for path in destination.rglob("*"):
        if path.is_symlink():
            continue
        expected = 0o700 if path.is_dir() else 0o600
        assert stat.S_IMODE(path.stat().st_mode) == expected

    verified = verify_private_paths(destination)
    assert verified.destination == destination
    repeated = migrate_private_paths(destination, managed=lambda _path: False, home=tmp_path)
    assert repeated.already_complete is True


def test_apply_output_contains_no_env_value(
    private_sources: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "Private" / "jobrunner"

    assert main(["--destination", str(destination), "--apply"]) == 0

    output = capsys.readouterr().out
    assert "copied and verified" in output
    assert "JOBRUNNER_ENV_FILE=" in output
    assert "must-never-be-printed" not in output


def test_verification_detects_a_changed_copy(
    private_sources: dict[str, Path], tmp_path: Path
) -> None:
    destination = tmp_path / "Private" / "jobrunner"
    migrate_private_paths(destination, managed=lambda _path: False, home=tmp_path)
    (destination / "storage" / "resume.pdf").write_bytes(b"changed")

    with pytest.raises(PrivatePathError, match="manifest"):
        verify_private_paths(destination)


def test_verification_detects_relaxed_permissions(
    private_sources: dict[str, Path], tmp_path: Path
) -> None:
    destination = tmp_path / "Private" / "jobrunner"
    migrate_private_paths(destination, managed=lambda _path: False, home=tmp_path)
    (destination / "storage" / "resume.pdf").chmod(0o644)

    with pytest.raises(PrivatePathError, match="permissions"):
        verify_private_paths(destination)


def test_a_source_change_during_copy_aborts_without_publishing(
    private_sources: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from packages.core import private_paths

    destination = tmp_path / "Private" / "jobrunner"
    original_copy = private_paths._copy_sources

    def copy_then_change(stage: Path, roots: dict[str, Path], env_file: Path) -> None:
        original_copy(stage, roots, env_file)
        (private_sources["storage"] / "resume.pdf").write_bytes(b"changed-during-copy")

    monkeypatch.setattr(private_paths, "_copy_sources", copy_then_change)

    with pytest.raises(PrivatePathError, match="changed while"):
        migrate_private_paths(destination, managed=lambda _path: False, home=tmp_path)
    assert not destination.exists()


def test_a_synced_destination_is_refused_before_any_write(
    private_sources: dict[str, Path], tmp_path: Path
) -> None:
    desktop = tmp_path / "Desktop"
    destination = desktop / "Private" / "jobrunner"

    with pytest.raises(PrivatePathError, match="sync"):
        plan_private_paths(
            destination,
            managed=lambda folder: folder == desktop,
            home=tmp_path,
        )
    assert not destination.exists()


def test_an_external_symlink_is_refused(private_sources: dict[str, Path], tmp_path: Path) -> None:
    outside = tmp_path / "outside-secret"
    outside.write_text("outside")
    (private_sources["storage"] / "escape").symlink_to(outside)

    with pytest.raises(PrivatePathError, match="symlink"):
        plan_private_paths(
            tmp_path / "Private" / "jobrunner",
            managed=lambda _path: False,
            home=tmp_path,
        )


def test_an_absolute_symlink_back_to_the_source_is_refused(
    private_sources: dict[str, Path], tmp_path: Path
) -> None:
    target = private_sources["storage"] / "resume.pdf"
    (private_sources["storage"] / "old-root-link").symlink_to(target)

    with pytest.raises(PrivatePathError, match="absolute symlink"):
        plan_private_paths(
            tmp_path / "Private" / "jobrunner",
            managed=lambda _path: False,
            home=tmp_path,
        )
