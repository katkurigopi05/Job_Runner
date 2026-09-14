"""Backups, and restores that prove them without touching the live installation.

The guard tests run everywhere. The round trip needs pg_dump and pg_restore —
host tools or the running Postgres container — and backs up the *test*
database into a temporary directory, restores it into a throwaway
`jobrunner_restore_*` database, and checks the throwaway is gone afterwards.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from packages.backup.manifest import FORMAT_VERSION, Manifest, ManifestError
from packages.backup.tools import resolve_tools
from packages.backup.verify import RefusedTarget, guard_targets

LIVE = "postgresql+asyncpg://jobrunner:jobrunner@localhost:5433/jobrunner"


# --------------------------------------------------------------------------
# The guard: verification can never be pointed at the live installation
# --------------------------------------------------------------------------


def test_the_live_database_is_refused_as_a_target(tmp_path) -> None:
    with pytest.raises(RefusedTarget, match="live database"):
        guard_targets(
            live_url=LIVE,
            target_database="jobrunner",
            live_storage=tmp_path / "storage",
            target_storage=tmp_path / "verify",
        )


def test_only_restore_database_names_are_accepted(tmp_path) -> None:
    with pytest.raises(RefusedTarget, match="not a restore database name"):
        guard_targets(
            live_url=LIVE,
            target_database="jobrunner_test",
            live_storage=tmp_path / "storage",
            target_storage=tmp_path / "verify",
        )


@pytest.mark.parametrize("target", ["storage", "storage/resumes"])
def test_storage_inside_or_equal_to_live_is_refused(tmp_path, target) -> None:
    (tmp_path / "storage").mkdir(exist_ok=True)
    with pytest.raises(RefusedTarget, match="overlaps"):
        guard_targets(
            live_url=LIVE,
            target_database="jobrunner_restore_x",
            live_storage=tmp_path / "storage",
            target_storage=tmp_path / target,
        )


def test_storage_containing_live_is_refused(tmp_path) -> None:
    with pytest.raises(RefusedTarget, match="overlaps"):
        guard_targets(
            live_url=LIVE,
            target_database="jobrunner_restore_x",
            live_storage=tmp_path / "home" / "storage",
            target_storage=tmp_path / "home",
        )


def test_an_unknown_manifest_version_is_refused(tmp_path) -> None:
    (tmp_path / "manifest.json").write_text('{"format_version": 99}')

    with pytest.raises(ManifestError, match="format 99"):
        Manifest.read(tmp_path)


# --------------------------------------------------------------------------
# The round trip
# --------------------------------------------------------------------------


def _storage(root: Path) -> Path:
    storage = root / "storage"
    (storage / "resumes").mkdir(parents=True)
    (storage / "resumes" / "base.pdf").write_bytes(b"%PDF-1.7 not really")
    (storage / "receipts").mkdir()
    (storage / "receipts" / "r1.png").write_bytes(b"\x89PNG screenshot")
    (storage / "browser" / "greenhouse").mkdir(parents=True)
    (storage / "browser" / "greenhouse" / "Cookies").write_bytes(b"session=secret-cookie")
    (storage / "logs").mkdir()
    (storage / "logs" / "worker.log").write_text("log line")
    return storage


async def _databases(url: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return set(
                (await connection.execute(text("SELECT datname FROM pg_database"))).scalars().all()
            )
    finally:
        await engine.dispose()


async def test_a_backup_restores_into_isolation_and_matches_its_manifest(
    tmp_path, db_session
) -> None:
    from packages.backup.create import create_backup
    from packages.backup.verify import verify_restore
    from tests.conftest import TEST_DATABASE_URL

    tools = await asyncio.to_thread(resolve_tools)
    if tools is None:
        pytest.skip("no pg_dump/pg_restore on the host and no running Postgres container")

    storage = _storage(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "greenhouse.enc").write_bytes(b"ciphertext")
    key = "not-a-real-key-" * 3

    destination, manifest = await create_backup(
        database_url=TEST_DATABASE_URL,
        storage_root=storage,
        vault_root=vault,
        dest_root=tmp_path / "backups",
        tools=tools,
        vault_key=key,
        now=datetime(2026, 9, 14, 18, 0, tzinfo=UTC),
    )

    assert manifest.format_version == FORMAT_VERSION
    assert {entry.path for entry in manifest.artifacts} == {"resumes/base.pdf", "receipts/r1.png"}
    assert set(manifest.excluded) == {"browser", "logs"}
    assert manifest.vault["included"] is False and manifest.vault["key_included"] is False
    assert not (destination / "vault").exists()
    written = b"".join(path.read_bytes() for path in destination.rglob("*") if path.is_file())
    assert b"secret-cookie" not in written, "browser profiles are not backed up by default"
    assert key.encode() not in written, "the vault key never reaches a backup"

    report = await verify_restore(
        destination, database_url=TEST_DATABASE_URL, storage_root=storage, tools=tools
    )

    assert report.ok, report.checks
    assert {check.name for check in report.checks} == {
        "dump checksum",
        "restore",
        "row counts",
        "schema revision",
        "artifacts",
    }
    assert report.database.startswith("jobrunner_restore_")
    assert report.database not in await _databases(TEST_DATABASE_URL), "throwaway database dropped"
    assert not Path(report.storage).exists(), "throwaway storage removed"
    assert (destination / "verification.json").is_file()


async def test_a_tampered_dump_fails_verification_before_restoring(tmp_path, db_session) -> None:
    from packages.backup.create import create_backup
    from packages.backup.verify import verify_restore
    from tests.conftest import TEST_DATABASE_URL

    tools = await asyncio.to_thread(resolve_tools)
    if tools is None:
        pytest.skip("no pg_dump/pg_restore on the host and no running Postgres container")

    storage = _storage(tmp_path)
    destination, _ = await create_backup(
        database_url=TEST_DATABASE_URL,
        storage_root=storage,
        vault_root=tmp_path / "vault",
        dest_root=tmp_path / "backups",
        tools=tools,
    )
    with (destination / "database.dump").open("ab") as handle:
        handle.write(b"tampered")

    report = await verify_restore(
        destination, database_url=TEST_DATABASE_URL, storage_root=storage, tools=tools
    )

    assert not report.ok
    assert [check.name for check in report.checks] == ["dump checksum"]


def test_the_setup_page_asks_for_a_first_backup(tmp_path) -> None:
    from apps.api.setup_status import backup_item

    item = backup_item(tmp_path / "none", datetime.now(UTC))

    assert item.state == "attention"
    assert item.steps[0] == "make backup"
