"""Provision a least-privilege database login for API and worker processes."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from packages.core.database_access import async_database_url


class RolePlanError(ValueError):
    """A requested role boundary is ambiguous or unsafe."""


class RoleProvisionError(RuntimeError):
    """The role boundary could not be applied without exposing credentials."""


@dataclass(frozen=True)
class DatabaseRolePlan:
    role: str
    database: str
    maintenance_role: str
    statements: tuple[str, ...]
    password: str = field(repr=False)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _location(url: URL) -> tuple[str, int, str | None]:
    return ((url.host or "localhost").casefold(), url.port or 5432, url.database)


def database_role_plan(
    maintenance_url: str,
    runtime_url: str,
    *,
    denied_databases: Sequence[str] = (),
) -> DatabaseRolePlan:
    """Validate two credentials and build the non-secret grant statements."""
    try:
        maintenance = make_url(maintenance_url)
        runtime = make_url(runtime_url)
    except ArgumentError as exc:
        raise RolePlanError("maintenance and runtime database URLs must be valid") from exc
    if _location(maintenance) != _location(runtime):
        raise RolePlanError("maintenance and runtime URLs must name the same server and database")
    if not maintenance.username or not runtime.username or not runtime.password:
        raise RolePlanError("both URLs need users and the runtime URL needs a password")
    if maintenance.username == runtime.username:
        raise RolePlanError("the runtime role must differ from the maintenance role")
    if not runtime.database:
        raise RolePlanError("the runtime URL must name a database")
    if runtime.database in denied_databases:
        raise RolePlanError("the runtime database cannot also be denied")

    role = _quote(runtime.username)
    database = _quote(runtime.database)
    owner = _quote(maintenance.username)
    statements = [
        f"REVOKE ALL PRIVILEGES ON DATABASE {database} FROM {role}",
        f"REVOKE CONNECT, CREATE, TEMPORARY ON DATABASE {database} FROM PUBLIC",
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
        f"REVOKE ALL PRIVILEGES ON SCHEMA public FROM {role}",
        f"GRANT CONNECT ON DATABASE {database} TO {role}",
        f"GRANT USAGE ON SCHEMA public TO {role}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}",
        f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {role}",
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}",
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
        f"GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {role}",
    ]
    for denied in denied_databases:
        denied_database = _quote(denied)
        statements.extend(
            (
                f"REVOKE CONNECT ON DATABASE {denied_database} FROM PUBLIC",
                f"REVOKE CONNECT ON DATABASE {denied_database} FROM {role}",
            )
        )
    return DatabaseRolePlan(
        role=runtime.username,
        database=runtime.database,
        maintenance_role=maintenance.username,
        statements=tuple(statements),
        password=runtime.password,
    )


_SET_ROLE_CREDENTIAL = text(
    "SELECT "
    "set_config('jobrunner.runtime_role', :role, true), "
    "set_config('jobrunner.runtime_password', :password, true)"
)

_CREATE_OR_UPDATE_ROLE = text(
    """
    DO $jobrunner$
    DECLARE
        runtime_role text := current_setting('jobrunner.runtime_role');
        runtime_password text := current_setting('jobrunner.runtime_password');
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = runtime_role) THEN
            EXECUTE format(
                'ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                'NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD %L',
                runtime_role,
                runtime_password
            );
        ELSE
            EXECUTE format(
                'CREATE ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                'NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD %L',
                runtime_role,
                runtime_password
            );
        END IF;
    END
    $jobrunner$
    """
)


async def _refuse_elevated_or_member_role(connection: AsyncConnection, role: str) -> None:
    role_row = (
        await connection.execute(
            text(
                "SELECT oid, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
                "FROM pg_roles WHERE rolname = :role"
            ),
            {"role": role},
        )
    ).one_or_none()
    if role_row is None:
        return
    role_oid, *attributes = role_row
    if any(attributes):
        raise RolePlanError("the existing runtime role is elevated; refusing to alter it")
    membership = await connection.scalar(
        text(
            "SELECT EXISTS ("
            "SELECT 1 FROM pg_auth_members membership "
            "JOIN pg_roles member ON member.oid = membership.member "
            "WHERE member.rolname = :role)"
        ),
        {"role": role},
    )
    if membership:
        raise RolePlanError("the existing runtime role has memberships; refusing to alter it")
    owns_objects = await connection.scalar(
        text(
            "SELECT EXISTS ("
            "SELECT 1 FROM pg_database WHERE datdba = :role_oid "
            "UNION ALL SELECT 1 FROM pg_namespace WHERE nspowner = :role_oid "
            "UNION ALL SELECT 1 FROM pg_class WHERE relowner = :role_oid "
            "UNION ALL SELECT 1 FROM pg_proc WHERE proowner = :role_oid "
            "UNION ALL SELECT 1 FROM pg_type WHERE typowner = :role_oid)"
        ),
        {"role_oid": role_oid},
    )
    if owns_objects:
        raise RolePlanError("the existing runtime role owns database objects; refusing to alter it")


async def provision_runtime_role(
    maintenance_url: str,
    runtime_url: str,
    *,
    denied_databases: Sequence[str] = (),
) -> DatabaseRolePlan:
    """Create or refresh the runtime role and its least-privilege grants."""
    plan = database_role_plan(
        maintenance_url,
        runtime_url,
        denied_databases=denied_databases,
    )
    engine = create_async_engine(async_database_url(maintenance_url), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            current_user = await connection.scalar(text("SELECT current_user"))
            if current_user != plan.maintenance_role:
                raise RolePlanError("maintenance URL authenticated as an unexpected role")
            await _refuse_elevated_or_member_role(connection, plan.role)
            await connection.execute(
                _SET_ROLE_CREDENTIAL,
                {"role": plan.role, "password": plan.password},
            )
            await connection.execute(_CREATE_OR_UPDATE_ROLE)
            for statement in plan.statements:
                await connection.execute(text(statement))
    except RolePlanError:
        raise
    except SQLAlchemyError as exc:
        raise RoleProvisionError(
            "database role provisioning failed; the transaction was rolled back"
        ) from exc
    finally:
        await engine.dispose()
    return plan


__all__ = [
    "DatabaseRolePlan",
    "RolePlanError",
    "RoleProvisionError",
    "database_role_plan",
    "provision_runtime_role",
]
