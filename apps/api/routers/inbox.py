"""Inbound message routes.

`packages/inbox/` already polls IMAP, matches the `+app{id}` alias back to an
application, classifies the reply, and moves the application's outcome. None of
that was reachable from outside the worker, so a recruiter reply could change an
application and leave no way to see what it said.

Messages are somebody else's correspondence. They stay on this machine — the API
is loopback-only (CLAUDE.md §2.8, and apps/api/middleware.py).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query
from sqlalchemy import select

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from packages.core.enums import ErrorCode
from packages.core.models import Application, InboundMessage
from packages.core.schemas import InboundMessageOut

router = APIRouter(prefix="/inbox", tags=["inbox"])

#: A mailbox grows without bound; a page of it does not.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500


@router.get("", response_model=list[InboundMessageOut])
async def list_messages(
    session: SessionDep,
    candidate_id: uuid.UUID | None = None,
    application_id: uuid.UUID | None = None,
    classification: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> list[InboundMessage]:
    """Recruiter replies, newest first.

    Filters are optional and combine. `application_id` is the one that answers
    the question the tracker actually asks — "what did they say about this
    application?".
    """
    stmt = select(InboundMessage).order_by(InboundMessage.at.desc()).limit(limit)

    if candidate_id is not None:
        stmt = stmt.where(InboundMessage.candidate_id == candidate_id)
    if application_id is not None:
        stmt = stmt.where(InboundMessage.application_id == application_id)
    if classification is not None:
        stmt = stmt.where(InboundMessage.classification == classification)

    return list((await session.scalars(stmt)).all())


@router.get("/unrouted", response_model=list[InboundMessageOut])
async def list_unrouted(
    session: SessionDep,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> list[InboundMessage]:
    """Messages that arrived but matched no application.

    Worth surfacing rather than hiding: a reply that did not route is either a
    recruiter writing from an address the alias could not be read from, or a
    bug in alias matching. Both are things the owner wants to know about, and
    neither is visible from the application side by definition.
    """
    stmt = (
        select(InboundMessage)
        .where(InboundMessage.application_id.is_(None))
        .order_by(InboundMessage.at.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


@router.get("/for-application/{application_id}", response_model=list[InboundMessageOut])
async def list_for_application(
    application_id: uuid.UUID, session: SessionDep
) -> list[InboundMessage]:
    """The thread for one application, oldest first so it reads as an exchange."""
    application = await session.get(Application, application_id)
    if application is None:
        raise ApiError(ErrorCode.NOT_FOUND, "application not found")

    stmt = (
        select(InboundMessage)
        .where(InboundMessage.application_id == application_id)
        .order_by(InboundMessage.at.asc())
    )
    return list((await session.scalars(stmt)).all())
