"""Candidate routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status
from sqlalchemy import select

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from packages.core.enums import ErrorCode
from packages.core.models import Candidate, User
from packages.core.schemas import CandidateCreate, CandidateOut

router = APIRouter(prefix="/candidates", tags=["candidates"])

#: Single-user product. The owner row is created on first use rather than
#: through a signup flow that does not exist. CLAUDE.md §11.
OWNER_SENTINEL_EMAIL = "owner@localhost"


async def _owner(session: SessionDep) -> User:
    user = await session.scalar(select(User).where(User.email == OWNER_SENTINEL_EMAIL))
    if user is None:
        user = User(email=OWNER_SENTINEL_EMAIL)
        session.add(user)
        await session.flush()
    return user


@router.post("", response_model=CandidateOut, status_code=status.HTTP_201_CREATED)
async def create_candidate(body: CandidateCreate, session: SessionDep) -> Candidate:
    owner = await _owner(session)
    candidate = Candidate(
        user_id=owner.id,
        name=body.name,
        email=str(body.email),
        email_mode=body.email_mode.value,
        managed_alias=body.managed_alias,
    )
    session.add(candidate)
    await session.commit()
    await session.refresh(candidate)
    return candidate


@router.get("", response_model=list[CandidateOut])
async def list_candidates(session: SessionDep) -> list[Candidate]:
    result = await session.scalars(select(Candidate).order_by(Candidate.created_at.desc()))
    return list(result.all())


@router.get("/{candidate_id}", response_model=CandidateOut)
async def get_candidate(candidate_id: uuid.UUID, session: SessionDep) -> Candidate:
    candidate = await session.get(Candidate, candidate_id)
    if candidate is None:
        raise ApiError(ErrorCode.NOT_FOUND, "candidate not found")
    return candidate
