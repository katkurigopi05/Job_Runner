"""Profile routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status
from sqlalchemy import select

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from packages.core.enums import ErrorCode
from packages.core.models import Candidate, Profile
from packages.core.schemas import ProfileCreate, ProfileOut

router = APIRouter(prefix="/profiles", tags=["profiles"])


@router.post("", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
async def create_profile(body: ProfileCreate, session: SessionDep) -> Profile:
    candidate = await session.get(Candidate, body.candidate_id)
    if candidate is None:
        raise ApiError(ErrorCode.INVALID_REQUEST, "candidate_id does not exist")

    profile = Profile(
        candidate_id=body.candidate_id,
        label=body.label,
        phone=body.phone,
        location=body.location,
        work_auth=body.work_auth,
        needs_sponsorship=body.needs_sponsorship,
        links_json=body.links,
        salary_expectation=body.salary_expectation,
        answers_kv_json=body.answers,
        min_match_score=body.min_match_score,
        auto_submit=body.auto_submit,
    )
    session.add(profile)
    await session.commit()
    await session.refresh(profile)
    return profile


@router.get("", response_model=list[ProfileOut])
async def list_profiles(session: SessionDep) -> list[Profile]:
    result = await session.scalars(select(Profile).order_by(Profile.created_at.desc()))
    return list(result.all())


@router.get("/{profile_id}", response_model=ProfileOut)
async def get_profile(profile_id: uuid.UUID, session: SessionDep) -> Profile:
    profile = await session.get(Profile, profile_id)
    if profile is None:
        raise ApiError(ErrorCode.NOT_FOUND, "profile not found")
    return profile
