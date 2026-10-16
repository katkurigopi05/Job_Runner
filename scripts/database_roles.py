"""Plan or provision the API/worker database role without printing secrets."""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy.engine import make_url

from packages.core.config import get_settings
from packages.core.database_access import required_maintenance_database_url
from packages.core.database_roles import (
    RolePlanError,
    RoleProvisionError,
    database_role_plan,
    provision_runtime_role,
)


def _location(url: str) -> str:
    parsed = make_url(url)
    return f"{parsed.host or 'localhost'}:{parsed.port or 5432}/{parsed.database}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the displayed role and grant boundary; the default is a dry run",
    )
    parser.add_argument(
        "--deny-database",
        action="append",
        default=[],
        help="also remove PUBLIC and runtime-role CONNECT from this environment database",
    )
    args = parser.parse_args(argv)

    try:
        runtime_url = get_settings().database_url
        maintenance_url = required_maintenance_database_url()
        plan = database_role_plan(
            maintenance_url,
            runtime_url,
            denied_databases=args.deny_database,
        )
    except (RolePlanError, ValueError) as exc:
        parser.error(str(exc))

    print(f"runtime role: {plan.role}")
    print(f"database: {_location(runtime_url)}")
    print(f"maintenance role: {plan.maintenance_role}")
    print("grants: connect, schema usage, table CRUD, sequence use")
    print("denied: database/schema creation, temporary tables, elevated role attributes")
    if args.deny_database:
        print("cross-environment connect denied: " + ", ".join(args.deny_database))
    if not args.apply:
        print("DRY RUN: no database changes made; rerun with apply=1 after backup and quiesce")
        return 0

    try:
        asyncio.run(
            provision_runtime_role(
                maintenance_url,
                runtime_url,
                denied_databases=args.deny_database,
            )
        )
    except (RolePlanError, RoleProvisionError) as exc:
        parser.error(str(exc))
    print("runtime database role provisioned; switch services only after the smoke checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
