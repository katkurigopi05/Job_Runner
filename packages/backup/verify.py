"""Restore a backup somewhere isolated and prove it matches its manifest."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from packages.backup.create import _counts, _inside, _revision
from packages.backup.manifest import (
    ARTIFACTS_DIR,
    DATABASE_FILE,
    VERIFICATION,
    Manifest,
    sha256_file,
)
from packages.backup.tools import PgTools, target_from_url

#: The only database names a verification may create. Nothing else — least of
#: all the live database — can be a restore target.
RESTORE_DATABASE = re.compile(r"^jobrunner_restore_[a-z0-9_]{1,40}$")


class RefusedTarget(Exception):
    """A restore target that could touch the live installation."""


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class VerifyReport:
    backup: str
    database: str
    storage: str
    checks: list[Check] = field(default_factory=list)
    kept: bool = False
    verified_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(check.ok for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "ok": self.ok}


def guard_targets(
    *, live_url: str, target_database: str, live_storage: Path, target_storage: Path
) -> None:
    """Refuse any target that is, contains, or sits inside the live installation."""
    live = make_url(live_url).database
    if target_database == live:
        raise RefusedTarget(f"{target_database!r} is the live database")
    if not RESTORE_DATABASE.match(target_database):
        raise RefusedTarget(
            f"{target_database!r} is not a restore database name (jobrunner_restore_<suffix>)"
        )
    for a, b in ((target_storage, live_storage), (live_storage, target_storage)):
        if _inside(a, b):
            raise RefusedTarget(f"{target_storage} overlaps the live storage at {live_storage}")


def _maintenance_url(url: str) -> str:
    return (
        make_url(url)
        .set(drivername="postgresql+asyncpg", database="postgres")
        .render_as_string(hide_password=False)
    )


def _database_url(url: str, database: str) -> str:
    return (
        make_url(url)
        .set(drivername="postgresql+asyncpg", database=database)
        .render_as_string(hide_password=False)
    )


async def _admin(url: str, statements: list[str]) -> None:
    engine = create_async_engine(_maintenance_url(url), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            for statement in statements:
                await connection.execute(text(statement))
    finally:
        await engine.dispose()


def _run_restore(tools: PgTools, url: str, database: str, dump: Path) -> str:
    target = target_from_url(url, database=database)
    command = tools.command(
        "pg_restore", target, ["--no-owner", "--exit-on-error", "--dbname", database]
    )
    with dump.open("rb") as handle:
        result = subprocess.run(
            command, stdin=handle, capture_output=True, env=tools.env(target), check=False
        )
    return "" if result.returncode == 0 else result.stderr.decode(errors="replace").strip()


async def verify_restore(
    backup_dir: Path,
    *,
    database_url: str,
    storage_root: Path,
    tools: PgTools,
    target_database: str | None = None,
    target_storage: Path | None = None,
    keep: bool = False,
) -> VerifyReport:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    database = target_database or f"jobrunner_restore_{stamp}"
    storage = target_storage or backup_dir.parent / f"_verify-{stamp}"
    guard_targets(
        live_url=database_url,
        target_database=database,
        live_storage=storage_root,
        target_storage=storage,
    )
    manifest = Manifest.read(backup_dir)
    report = VerifyReport(
        backup=str(backup_dir), database=database, storage=str(storage), kept=keep
    )
    dump = backup_dir / DATABASE_FILE

    actual = sha256_file(dump) if dump.is_file() else None
    report.checks.append(
        Check(
            "dump checksum",
            actual == manifest.database["sha256"],
            "matches the manifest"
            if actual == manifest.database["sha256"]
            else "the dump file differs from the manifest",
        )
    )
    if not report.checks[-1].ok:
        return _record(backup_dir, report)

    quoted = '"' + database + '"'
    created = False
    try:
        await _admin(database_url, [f"CREATE DATABASE {quoted}"])
        created = True
        error = await asyncio.to_thread(_run_restore, tools, database_url, database, dump)
        report.checks.append(Check("restore", not error, error or f"restored into {database}"))
        if not error:
            engine = create_async_engine(_database_url(database_url, database))
            try:
                async with engine.connect() as connection:
                    counts = await _counts(connection)
                    revision = await _revision(connection)
            finally:
                await engine.dispose()
            expected = manifest.database["tables"]
            mismatched = sorted(
                name
                for name in set(expected) | set(counts)
                if expected.get(name) != counts.get(name)
            )
            report.checks.append(
                Check(
                    "row counts",
                    not mismatched,
                    f"{len(expected)} tables match"
                    if not mismatched
                    else "differ: "
                    + ", ".join(
                        f"{name} {expected.get(name)}→{counts.get(name)}"
                        for name in mismatched[:10]
                    ),
                )
            )
            report.checks.append(
                Check(
                    "schema revision",
                    revision == manifest.alembic_revision,
                    f"at {revision}"
                    if revision == manifest.alembic_revision
                    else f"restored {revision}, manifest {manifest.alembic_revision}",
                )
            )

        storage.mkdir(parents=True, exist_ok=False)
        bad = []
        for entry in manifest.artifacts:
            source = backup_dir / ARTIFACTS_DIR / entry.path
            copy = storage / entry.path
            copy.parent.mkdir(parents=True, exist_ok=True)
            if not source.is_file():
                bad.append(f"{entry.path} missing")
                continue
            shutil.copy2(source, copy)
            if sha256_file(copy) != entry.sha256:
                bad.append(f"{entry.path} checksum")
        report.checks.append(
            Check(
                "artifacts",
                not bad,
                f"{len(manifest.artifacts)} files restored and match"
                if not bad
                else "problems: " + ", ".join(bad[:10]),
            )
        )
    finally:
        if not keep:
            if created:
                await _admin(
                    database_url,
                    [
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        f"WHERE datname = '{database}' AND pid <> pg_backend_pid()",
                        f"DROP DATABASE IF EXISTS {quoted}",
                    ],
                )
            shutil.rmtree(storage, ignore_errors=True)
    return _record(backup_dir, report)


def _record(backup_dir: Path, report: VerifyReport) -> VerifyReport:
    (backup_dir / VERIFICATION).write_text(json.dumps(report.as_dict(), indent=2) + "\n")
    return report


__all__ = [
    "RESTORE_DATABASE",
    "Check",
    "RefusedTarget",
    "VerifyReport",
    "guard_targets",
    "verify_restore",
]
