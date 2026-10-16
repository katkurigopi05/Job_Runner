"""The runtime role can use Jobrunner data without administering Postgres."""

from __future__ import annotations

import uuid

import pytest
from asyncpg import InsufficientPrivilegeError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from tests.conftest import REQUIRE_DB, TEST_DATABASE_URL


def _runtime_url(role: str, password: str) -> str:
    return make_url(TEST_DATABASE_URL).set(username=role, password=password).render_as_string(False)


async def _public_privileges(connection: AsyncConnection, object_type: str) -> set[str]:
    if object_type == "database":
        query = (
            "SELECT privilege_type FROM pg_database, "
            "LATERAL aclexplode(COALESCE(datacl, acldefault('d', datdba))) "
            "WHERE datname = current_database() AND grantee = 0"
        )
    else:
        query = (
            "SELECT privilege_type FROM pg_namespace, "
            "LATERAL aclexplode(COALESCE(nspacl, acldefault('n', nspowner))) "
            "WHERE nspname = 'public' AND grantee = 0"
        )
    return set((await connection.execute(text(query))).scalars())


def test_role_plan_refuses_the_maintenance_identity() -> None:
    from packages.core.database_roles import RolePlanError, database_role_plan

    with pytest.raises(RolePlanError, match="must differ"):
        database_role_plan(TEST_DATABASE_URL, TEST_DATABASE_URL)


def test_role_plan_refuses_another_server_or_database() -> None:
    from packages.core.database_roles import RolePlanError, database_role_plan

    maintenance = make_url(TEST_DATABASE_URL)
    role = f"jobrunner_app_{uuid.uuid4().hex[:8]}"
    other_database = maintenance.set(username=role, password="secret", database="somewhere_else")

    with pytest.raises(RolePlanError, match="same server and database"):
        database_role_plan(TEST_DATABASE_URL, other_database.render_as_string(False))


def test_role_plan_never_puts_the_password_in_sql() -> None:
    from packages.core.database_roles import database_role_plan

    password = "the-password-must-not-appear"
    role = f"jobrunner_app_{uuid.uuid4().hex[:8]}"

    plan = database_role_plan(TEST_DATABASE_URL, _runtime_url(role, password))

    assert password not in "\n".join(plan.statements)
    assert plan.role == role


@pytest.mark.skipif(not REQUIRE_DB, reason="requires the isolated PostgreSQL test database")
async def test_runtime_role_can_crud_but_cannot_administer_or_cross_environments(engine) -> None:
    from packages.core.database_roles import provision_runtime_role

    suffix = uuid.uuid4().hex[:10]
    role = f"jobrunner_app_{suffix}"
    password = f"runtime-{suffix}"
    probe = f"role_probe_{suffix}"
    future_probe = f"role_future_{suffix}"
    other_database = f"jobrunner_role_other_{suffix}"
    runtime_url = _runtime_url(role, password)
    test_database = make_url(TEST_DATABASE_URL).database
    assert test_database is not None
    assert engine.url.database == test_database  # marker and suite-wide lock were checked
    admin = create_async_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    app = create_async_engine(runtime_url)
    other = create_async_engine(make_url(runtime_url).set(database=other_database))
    original_database_privileges: set[str] | None = None
    original_schema_privileges: set[str] | None = None
    try:
        async with admin.connect() as connection:
            original_database_privileges = await _public_privileges(connection, "database")
            original_schema_privileges = await _public_privileges(connection, "schema")
            await connection.execute(
                text(
                    f'CREATE TABLE "{probe}" ('
                    "id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY, value integer)"
                )
            )
            await connection.execute(text(f'CREATE DATABASE "{other_database}"'))

        await provision_runtime_role(
            TEST_DATABASE_URL,
            runtime_url,
            denied_databases=(other_database,),
        )

        async with app.begin() as connection:
            assert await connection.scalar(text("SELECT current_user")) == role
            inserted = await connection.scalar(
                text(f'INSERT INTO "{probe}" (value) VALUES (1) RETURNING id')
            )
            await connection.execute(
                text(f'UPDATE "{probe}" SET value = 2 WHERE id = :id'), {"id": inserted}
            )
            assert (
                await connection.scalar(
                    text(f'SELECT value FROM "{probe}" WHERE id = :id'), {"id": inserted}
                )
                == 2
            )
            await connection.execute(
                text(f'DELETE FROM "{probe}" WHERE id = :id'), {"id": inserted}
            )

        refused_ddl = (
            "CREATE TABLE runtime_must_not_create (id int)",
            "CREATE TEMPORARY TABLE runtime_temp_must_not_create (id int)",
            "CREATE SCHEMA runtime_schema_must_not_create",
            f'ALTER TABLE "{probe}" ADD COLUMN runtime_must_not_add integer',
        )
        for statement in refused_ddl:
            async with app.connect() as connection:
                with pytest.raises(DBAPIError):
                    await connection.execute(text(statement))

        await app.dispose()
        app = create_async_engine(runtime_url)
        async with admin.connect() as connection:
            await connection.execute(
                text(
                    f'CREATE TABLE "{future_probe}" ('
                    "id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY)"
                )
            )
        async with app.begin() as connection:
            assert (
                await connection.scalar(
                    text(f'INSERT INTO "{future_probe}" DEFAULT VALUES RETURNING id')
                )
                == 1
            )

        with pytest.raises(InsufficientPrivilegeError):
            async with other.connect():
                pass
    finally:
        await app.dispose()
        await other.dispose()
        async with admin.connect() as connection:
            await connection.execute(
                text(
                    f'DROP TABLE IF EXISTS "{probe}", "{future_probe}", '
                    "runtime_must_not_create, runtime_temp_must_not_create"
                )
            )
            await connection.execute(text("DROP SCHEMA IF EXISTS runtime_schema_must_not_create"))
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{other_database}"'))
            await connection.execute(text(f'DROP OWNED BY "{role}"'))
            await connection.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
            if original_database_privileges is not None:
                await connection.execute(
                    text(
                        f'REVOKE CONNECT, CREATE, TEMPORARY ON DATABASE "{test_database}" '
                        "FROM PUBLIC"
                    )
                )
                if original_database_privileges:
                    grants = ", ".join(sorted(original_database_privileges))
                    await connection.execute(
                        text(f'GRANT {grants} ON DATABASE "{test_database}" TO PUBLIC')
                    )
            if original_schema_privileges is not None:
                await connection.execute(text("REVOKE USAGE, CREATE ON SCHEMA public FROM PUBLIC"))
                if original_schema_privileges:
                    grants = ", ".join(sorted(original_schema_privileges))
                    await connection.execute(text(f"GRANT {grants} ON SCHEMA public TO PUBLIC"))
        await admin.dispose()
