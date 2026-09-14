"""Explicit ranking preferences, and suggestions drawn from skip reasons.

Preferences adjust the order the feed shows when `rank=personalized` is asked
for; they never change `Match.score`. Suggestions are computed on request and
stored nowhere until the owner accepts one with a `PUT`.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Response
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from packages.core.enums import ErrorCode
from packages.core.models_ranking import RankingPreference
from packages.core.schemas_ranking import (
    RankingEvaluationOut,
    RankingPreferenceIn,
    RankingPreferenceOut,
    SuggestionOut,
)
from packages.matching.personalize import KINDS, suggest
from packages.matching.skill_vocab import normalize_skill

router = APIRouter(prefix="/ranking", tags=["ranking"])

_SCOPE = re.compile(
    r"^(?:global|profile:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


def _scope(scope: str) -> str:
    if not _SCOPE.match(scope):
        raise ApiError(ErrorCode.INVALID_REQUEST, "scope must be `global` or `profile:<uuid>`")
    return scope


def _normalized(body: RankingPreferenceIn) -> str:
    if body.kind not in KINDS:
        raise ApiError(ErrorCode.INVALID_REQUEST, f"kind must be one of {', '.join(KINDS)}")
    if body.source not in ("explicit", "suggestion"):
        raise ApiError(ErrorCode.INVALID_REQUEST, "source must be explicit or suggestion")
    value = body.value.strip()
    if body.kind == "skill":
        key = normalize_skill(value)
        if key is None:
            raise ApiError(ErrorCode.INVALID_REQUEST, f"not in the skill vocabulary: {value}")
        return key
    if body.kind == "remote" and value not in ("remote", "onsite"):
        raise ApiError(ErrorCode.INVALID_REQUEST, "remote preference is `remote` or `onsite`")
    return value


async def preferences_for(
    session: SessionDep, profile_id: uuid.UUID | None
) -> list[RankingPreference]:
    """Global preferences, with a profile's own overriding the same kind and value."""
    scopes = ["global"] + ([f"profile:{profile_id}"] if profile_id else [])
    rows = (
        await session.scalars(select(RankingPreference).where(RankingPreference.scope.in_(scopes)))
    ).all()
    chosen: dict[tuple[str, str], RankingPreference] = {}
    for row in sorted(rows, key=lambda r: r.scope != "global"):
        chosen[(row.kind, row.value.casefold())] = row
    return list(chosen.values())


@router.get("/preferences", response_model=list[RankingPreferenceOut])
async def list_preferences(
    session: SessionDep, scope: str = Query(default="global")
) -> list[RankingPreference]:
    rows = await session.scalars(
        select(RankingPreference)
        .where(RankingPreference.scope == _scope(scope))
        .order_by(RankingPreference.kind, RankingPreference.value)
    )
    return list(rows.all())


@router.put("/preferences", response_model=RankingPreferenceOut)
async def save_preference(
    body: RankingPreferenceIn, session: SessionDep, scope: str = Query(default="global")
) -> RankingPreference:
    value = _normalized(body)
    now = datetime.now(UTC)
    statement = pg_insert(RankingPreference).values(
        scope=_scope(scope),
        kind=body.kind,
        value=value,
        weight=body.weight,
        source=body.source,
        note=body.note,
        updated_at=now,
    )
    statement = statement.on_conflict_do_update(
        constraint="uq_ranking_preferences_scope_kind_value",
        set_={
            "weight": statement.excluded.weight,
            "source": statement.excluded.source,
            "note": statement.excluded.note,
            "updated_at": now,
        },
    )
    await session.execute(statement)
    await session.commit()
    saved = await session.scalar(
        select(RankingPreference)
        .where(
            RankingPreference.scope == scope,
            RankingPreference.kind == body.kind,
            RankingPreference.value == value,
        )
        .execution_options(populate_existing=True)
    )
    assert saved is not None
    return saved


@router.delete("/preferences/{preference_id}", status_code=204)
async def delete_preference(preference_id: uuid.UUID, session: SessionDep) -> Response:
    found = await session.get(RankingPreference, preference_id)
    if found is not None:
        await session.delete(found)
        await session.commit()
    return Response(status_code=204)


@router.get("/suggestions", response_model=list[SuggestionOut])
async def suggestions(
    session: SessionDep, profile_id: uuid.UUID | None = None
) -> list[SuggestionOut]:
    existing = await preferences_for(session, profile_id)
    found = await suggest(session, profile_id=profile_id, existing=existing)
    return [SuggestionOut(**item.as_dict()) for item in found]


@router.get("/evaluation", response_model=RankingEvaluationOut)
async def evaluation_report(
    session: SessionDep,
    profile_id: uuid.UUID | None = None,
    k: int = Query(default=10, ge=1, le=50),
) -> RankingEvaluationOut:
    """Held-out NDCG for the base and personalized orders, or why it cannot be said.

    Resolves the profile when there is exactly one, and refuses to guess when
    there are several — the same rule `/labels/next` follows.
    """
    from packages.core.models import Profile
    from packages.matching.evaluation import evaluate

    if profile_id is None:
        ids = list((await session.scalars(select(Profile.id))).all())
        if len(ids) != 1:
            raise ApiError(
                ErrorCode.INVALID_REQUEST, f"pass profile_id; there are {len(ids)} profiles"
            )
        profile_id = ids[0]
    elif await session.get(Profile, profile_id) is None:
        raise ApiError(ErrorCode.NOT_FOUND, "profile not found")

    preferences = await preferences_for(session, profile_id)
    report = await evaluate(session, profile_id, preferences, k=k)
    return RankingEvaluationOut(**report.as_dict())
