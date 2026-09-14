"""Back up the database and local artifacts, and verify a backup by restoring it.

    make backup                      # database + résumés, receipts, diagnostics
    make backup vault=1              # also vault ciphertext (never the key)
    make backup-verify dir=backups/jobrunner-20260914T180000Z

Verification restores into a throwaway `jobrunner_restore_*` database and a
throwaway directory, compares checksums, row counts and the schema revision
against the manifest, then deletes both (`keep=1` to inspect them). It refuses
any target that is the live database or overlaps live storage.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path


def _tools():  # type: ignore[no-untyped-def]
    from packages.backup.tools import resolve_tools

    tools = resolve_tools()
    if tools is None:
        raise SystemExit(
            "no pg_dump/pg_restore: install the PostgreSQL 16 client tools, or start the "
            "Postgres container (make up)"
        )
    return tools


def _create(args: argparse.Namespace) -> int:
    from packages.backup.create import create_backup
    from packages.core.config import get_settings

    settings = get_settings()
    tools = _tools()
    destination, manifest = asyncio.run(
        create_backup(
            database_url=settings.database_url,
            storage_root=Path(settings.storage_root),
            vault_root=Path(settings.vault_root),
            dest_root=Path(args.dest or settings.backup_root),
            tools=tools,
            include_vault_ciphertext=args.include_vault_ciphertext,
            include_browser_profiles=args.include_browser_profiles,
            vault_key=settings.vault_key or os.environ.get("VAULT_KEY"),
        )
    )
    rows = sum(manifest.database["tables"].values())
    tables = len(manifest.database["tables"])
    excluded = ", ".join(manifest.excluded) or "none"
    vault = "included" if manifest.vault["included"] else "not included"
    print(f"backup written to {destination} using {tools.describe()}")
    print(f"  database: {manifest.database['bytes']:,} bytes, {tables} tables, {rows:,} rows")
    print(f"  schema revision: {manifest.alembic_revision}")
    print(f"  artifacts: {len(manifest.artifacts)} files; excluded: {excluded}")
    print(f"  vault ciphertext: {vault}; vault key: never included")
    print(f"verify it:  make backup-verify dir={destination}")
    return 0


def _verify(args: argparse.Namespace) -> int:
    from packages.backup.verify import verify_restore
    from packages.core.config import get_settings

    settings = get_settings()
    report = asyncio.run(
        verify_restore(
            Path(args.dir),
            database_url=settings.database_url,
            storage_root=Path(settings.storage_root),
            tools=_tools(),
            keep=args.keep,
        )
    )
    for check in report.checks:
        print(f"  [{'ok' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
    kept = (
        f" (kept: {report.database}, {report.storage})"
        if report.kept
        else " (restore targets removed)"
    )
    print(("VERIFIED" if report.ok else "NOT VERIFIED") + kept)
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--dest", default=None)
    create.add_argument("--include-vault-ciphertext", action="store_true")
    create.add_argument("--include-browser-profiles", action="store_true")
    verify = sub.add_parser("verify")
    verify.add_argument("dir")
    verify.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)
    return _create(args) if args.command == "create" else _verify(args)


if __name__ == "__main__":
    raise SystemExit(main())
