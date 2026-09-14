"""Saved feed searches — kept apart from the profile, and read by nothing that applies.

`PUT /search-preferences/default` stores the search the Matches page opens
with. Filters only change the feed: no route that fills or submits an
application reads this table, which is the separation CLAUDE.md §1 asks for.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Response
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from packages.core.enums import ErrorCode
from packages.core.models_search import SearchPreference
from packages.core.schemas_search import SearchPreferenceIn, SearchPreferenceOut

router = APIRouter(prefix="/search-preferences", tags=["search"])

_SCOPE = re.compile(
    r"^(?:global|profile:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
_NAME = re.compile(r"^[A-Za-z0-9 _-]{1,100}$")


def _check(scope: str, name: str | None = None) -> None:
    if not _SCOPE.match(scope):
        raise ApiError(ErrorCode.INVALID_REQUEST, "scope must be `global` or `profile:<uuid>`")
    if name is not None and not _NAME.match(name):
        raise ApiError(
            ErrorCode.INVALID_REQUEST,
            "name may use letters, digits, spaces, dashes and underscores (1–100)",
        )


@router.get("", response_model=list[SearchPreferenceOut])
async def list_preferences(
    session: SessionDep, scope: str = Query(default="global")
) -> list[SearchPreference]:
    _check(scope)
    rows = await session.scalars(
        select(SearchPreference)
        .where(SearchPreference.scope == scope)
        .order_by(SearchPreference.name)
    )
    return list(rows.all())


@router.get("/{name}", response_model=SearchPreferenceOut)
async def get_preference(
    name: str, session: SessionDep, scope: str = Query(default="global")
) -> SearchPreference:
    _check(scope, name)
    found = await session.scalar(
        select(SearchPreference).where(
            SearchPreference.scope == scope, SearchPreference.name == name
        )
    )
    if found is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"no saved search {name!r}")
    return found


@router.put("/{name}", response_model=SearchPreferenceOut)
async def save_preference(
    name: str,
    body: SearchPreferenceIn,
    session: SessionDep,
    scope: str = Query(default="global"),
) -> SearchPreference:
    _check(scope, name)
    now = datetime.now(UTC)
    statement = pg_insert(SearchPreference).values(
        scope=scope, name=name, filters_json=body.filters, updated_at=now
    )
    statement = statement.on_conflict_do_update(
        constraint="uq_search_preferences_scope_name",
        set_={"filters_json": statement.excluded.filters_json, "updated_at": now},
    )
    await session.execute(statement)
    await session.commit()
    saved = await session.scalar(
        select(SearchPreference)
        .where(SearchPreference.scope == scope, SearchPreference.name == name)
        .execution_options(populate_existing=True)
    )
    assert saved is not None
    return saved


@router.delete("/{name}", status_code=204)
async def delete_preference(
    name: str, session: SessionDep, scope: str = Query(default="global")
) -> Response:
    _check(scope, name)
    found = await session.scalar(
        select(SearchPreference).where(
            SearchPreference.scope == scope, SearchPreference.name == name
        )
    )
    if found is not None:
        await session.delete(found)
        await session.commit()
    return Response(status_code=204)
