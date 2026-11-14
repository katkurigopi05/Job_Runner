"""Profile routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status
from sqlalchemy import delete, select

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from packages.core.enums import ErrorCode
from packages.core.models import Candidate, Profile
from packages.core.models_evidence import Certification, Internship
from packages.core.schemas import ProfileCreate, ProfileOut, ProfileUpdate
from packages.core.schemas_studio import (
    CertificationOut,
    EvidenceOut,
    EvidenceSave,
    InternshipOut,
)

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
        target_seniority=body.target_seniority,
        max_required_experience_years=body.max_required_experience_years,
        citizenship_status=body.citizenship_status,
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


#: Maps request field to model attribute where the two names differ.
_UPDATE_FIELDS = {
    "label": "label",
    "phone": "phone",
    "location": "location",
    "work_auth": "work_auth",
    "needs_sponsorship": "needs_sponsorship",
    "links": "links_json",
    "salary_expectation": "salary_expectation",
    "answers": "answers_kv_json",
    "min_match_score": "min_match_score",
    "auto_submit": "auto_submit",
    "target_seniority": "target_seniority",
    "max_required_experience_years": "max_required_experience_years",
    "citizenship_status": "citizenship_status",
}


@router.patch("/{profile_id}", response_model=ProfileOut)
async def update_profile(
    profile_id: uuid.UUID, body: ProfileUpdate, session: SessionDep
) -> Profile:
    """Edit a profile in place.

    `exclude_unset` rather than `exclude_none`: a field the caller did not send
    is left alone, but one sent explicitly as null is cleared. Those are
    different intentions and the dashboard relies on both — clearing an answer
    has to be possible, and a form that posts three fields must not blank the
    other seven.
    """
    profile = await session.get(Profile, profile_id)
    if profile is None:
        raise ApiError(ErrorCode.NOT_FOUND, "profile not found")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(profile, _UPDATE_FIELDS[field], value)

    await session.commit()
    await session.refresh(profile)
    return profile


async def _evidence(session: SessionDep, candidate_id: uuid.UUID) -> EvidenceOut:
    internships = await session.scalars(
        select(Internship)
        .where(Internship.candidate_id == candidate_id)
        .order_by(Internship.position, Internship.created_at)
    )
    certifications = await session.scalars(
        select(Certification)
        .where(Certification.candidate_id == candidate_id)
        .order_by(Certification.position, Certification.created_at)
    )
    return EvidenceOut(
        candidate_id=candidate_id,
        internships=[InternshipOut.model_validate(row) for row in internships.all()],
        certifications=[CertificationOut.model_validate(row) for row in certifications.all()],
    )


@router.get("/{profile_id}/evidence", response_model=EvidenceOut)
async def get_evidence(profile_id: uuid.UUID, session: SessionDep) -> EvidenceOut:
    """The internships and certifications the owner has stated, confirmed or not."""
    profile = await session.get(Profile, profile_id)
    if profile is None:
        raise ApiError(ErrorCode.NOT_FOUND, "profile not found")
    return await _evidence(session, profile.candidate_id)


@router.put("/{profile_id}/evidence", response_model=EvidenceOut)
async def save_evidence(
    profile_id: uuid.UUID, body: EvidenceSave, session: SessionDep
) -> EvidenceOut:
    """Replace the profile's candidate's internships and certifications with these.

    The whole list, in one transaction, so the screen and the tables cannot
    disagree about what was removed. Only the dashboard writes these: a
    record is what lets an internship or a certification reach a résumé, so
    no MCP tool can (CLAUDE.md §2.1).
    """
    profile = await session.get(Profile, profile_id)
    if profile is None:
        raise ApiError(ErrorCode.NOT_FOUND, "profile not found")
    candidate_id = profile.candidate_id
    await session.execute(delete(Internship).where(Internship.candidate_id == candidate_id))
    await session.execute(delete(Certification).where(Certification.candidate_id == candidate_id))
    session.add_all(
        Internship(candidate_id=candidate_id, position=index, **item.model_dump())
        for index, item in enumerate(body.internships)
    )
    session.add_all(
        Certification(candidate_id=candidate_id, position=index, **item.model_dump())
        for index, item in enumerate(body.certifications)
    )
    await session.commit()
    return await _evidence(session, candidate_id)
