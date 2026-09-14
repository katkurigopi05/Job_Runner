"""Create a backup directory: database dump, artifacts, manifest."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from packages.backup.manifest import (
    ARTIFACTS_DIR,
    DATABASE_FILE,
    EXCLUDED_BY_DEFAULT,
    VAULT_DIR,
    FileEntry,
    Manifest,
    sha256_file,
)
from packages.backup.tools import PgTools, target_from_url


class BackupError(Exception):
    """The backup could not be completed; nothing partial should be trusted."""


def _async_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def git_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


async def _counts(connection) -> dict[str, int]:  # type: ignore[no-untyped-def]
    tables = (
        (
            await connection.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
                )
            )
        )
        .scalars()
        .all()
    )
    counts: dict[str, int] = {}
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        counts[table] = int(
            (await connection.execute(text(f"SELECT count(*) FROM {quoted}"))).scalar_one()
        )
    return counts


async def _revision(connection) -> str | None:  # type: ignore[no-untyped-def]
    exists = (
        await connection.execute(text("SELECT to_regclass('public.alembic_version') IS NOT NULL"))
    ).scalar_one()
    if not exists:
        return None
    value = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    return str(value) if value is not None else None


def _run_dump(tools: PgTools, url: str, snapshot: str, destination: Path) -> None:
    target = target_from_url(url)
    command = tools.command(
        "pg_dump",
        target,
        ["--format=custom", f"--snapshot={snapshot}", "--dbname", target.database],
    )
    with destination.open("wb") as handle:
        result = subprocess.run(
            command, stdout=handle, stderr=subprocess.PIPE, env=tools.env(target), check=False
        )
    if result.returncode != 0:
        # pg_dump's stderr names the database and host, never the password.
        raise BackupError(f"pg_dump failed: {result.stderr.decode(errors='replace').strip()}")


def _copy_tree(source: Path, destination: Path, relative_to: Path) -> list[FileEntry]:
    entries: list[FileEntry] = []
    for path in sorted(p for p in source.rglob("*") if p.is_file() and not p.is_symlink()):
        relative = path.relative_to(relative_to)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        entries.append(FileEntry(str(relative), sha256_file(target), target.stat().st_size))
    return entries


def _vault_check(key: str | None) -> str | None:
    """A check value that says whether a key matches, and reveals nothing of it."""
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet

        Fernet(key.encode())
    except Exception:  # noqa: BLE001 - an invalid key has no meaningful check value
        return None
    return hashlib.sha256(b"jobrunner-vault-check:" + key.encode()).hexdigest()[:16]


async def create_backup(
    *,
    database_url: str,
    storage_root: Path,
    vault_root: Path,
    dest_root: Path,
    tools: PgTools,
    include_vault_ciphertext: bool = False,
    include_browser_profiles: bool = False,
    vault_key: str | None = None,
    now: datetime | None = None,
) -> tuple[Path, Manifest]:
    """Write one backup directory under `dest_root` and return it with its manifest."""
    moment = now or datetime.now(UTC)
    if _inside(dest_root, storage_root) or _inside(dest_root, vault_root):
        raise BackupError("the backup destination must not be inside storage or the vault root")
    destination = dest_root / f"jobrunner-{moment.strftime('%Y%m%dT%H%M%SZ')}"
    destination.mkdir(parents=True, exist_ok=False)
    os.chmod(destination, 0o700)

    engine = create_async_engine(_async_url(database_url))
    try:
        async with engine.connect() as connection:
            # One snapshot for the counts and the dump, so a worker writing
            # during the backup cannot make verification fail on a count the
            # dump never contained.
            await connection.execute(text("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            snapshot = (await connection.execute(text("SELECT pg_export_snapshot()"))).scalar_one()
            counts = await _counts(connection)
            revision = await _revision(connection)
            dump = destination / DATABASE_FILE
            await asyncio.to_thread(_run_dump, tools, database_url, snapshot, dump)
            await connection.execute(text("COMMIT"))
    finally:
        await engine.dispose()

    excluded = dict(EXCLUDED_BY_DEFAULT)
    if include_browser_profiles:
        excluded.pop("browser", None)
    artifacts: list[FileEntry] = []
    if storage_root.is_dir():
        for child in sorted(storage_root.iterdir()):
            if child.is_dir() and child.name not in excluded:
                artifacts += _copy_tree(child, destination / ARTIFACTS_DIR, storage_root)

    vault: dict[str, object] = {
        "included": include_vault_ciphertext,
        "key_included": False,
        "note": "The vault key is never written to a backup. Keep it separately; "
        "without it, restored credentials are unreadable.",
    }
    if include_vault_ciphertext and vault_root.is_dir():
        files = sorted(vault_root.glob("*.enc"))
        entries = []
        for path in files:
            target = destination / VAULT_DIR / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            entries.append({"path": path.name, "sha256": sha256_file(target)})
        vault.update({"files": entries, "key_check": _vault_check(vault_key)})

    manifest = Manifest(
        created_at=moment.isoformat(),
        app_revision=git_revision(),
        alembic_revision=revision,
        database={
            "file": DATABASE_FILE,
            "format": "pg_dump custom",
            "sha256": sha256_file(dump),
            "bytes": dump.stat().st_size,
            "tables": counts,
            "source_database": target_from_url(database_url).database,
        },
        artifacts=artifacts,
        excluded=excluded,
        vault=vault,
    )
    manifest.write(destination)
    return destination, manifest


__all__ = ["BackupError", "create_backup", "git_revision"]
