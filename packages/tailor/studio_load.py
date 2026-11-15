"""What the Resume Studio reads from the database: the base résumé, the owner's
confirmed internships and certifications, and their repositories.

Only confirmed (`verified`) internships and certifications are read. An
unconfirmed record is a draft the owner is still typing, and nothing typed
but unconfirmed reaches a résumé.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Profile, Project, Resume
from packages.core.models_evidence import Certification, Internship
from packages.tailor.parse import ParsedResume
from packages.tailor.studio_sources import (
    CertificationFacts,
    InternshipFacts,
    Source,
    build_source,
)


class StudioInputError(ValueError):
    """A studio request names something the candidate does not have."""


def internship_facts(row: Internship) -> InternshipFacts:
    return InternshipFacts(
        id=str(row.id),
        company=row.company,
        role=row.role,
        started=row.started,
        ended=row.ended,
        location=row.location,
        stack=tuple(row.stack or ()),
        bullets=tuple(row.bullets or ()),
    )


def certification_facts(row: Certification) -> CertificationFacts:
    return CertificationFacts(
        id=str(row.id),
        name=row.name,
        issuer=row.issuer,
        issued=row.issued,
        expires=row.expires,
        credential_id=row.credential_id,
        skills=tuple(row.skills or ()),
    )


async def confirmed_evidence(
    session: AsyncSession,
    candidate_id: uuid.UUID,
    *,
    include_internships: bool = True,
    certification_ids: Sequence[uuid.UUID] | None = None,
) -> tuple[list[InternshipFacts], list[CertificationFacts]]:
    """The confirmed records to add. `certification_ids` None means all of them."""
    internships: list[InternshipFacts] = []
    if include_internships:
        rows = await session.scalars(
            select(Internship)
            .where(Internship.candidate_id == candidate_id, Internship.verified.is_(True))
            .order_by(Internship.position, Internship.created_at)
        )
        internships = [internship_facts(row) for row in rows.all()]

    query = select(Certification).where(
        Certification.candidate_id == candidate_id, Certification.verified.is_(True)
    )
    if certification_ids is not None:
        query = query.where(Certification.id.in_(list(certification_ids)))
    rows_c = await session.scalars(query.order_by(Certification.position, Certification.created_at))
    return internships, [certification_facts(row) for row in rows_c.all()]


async def resolve_profile(
    session: AsyncSession, candidate_id: uuid.UUID, profile_id: uuid.UUID | None
) -> Profile | None:
    if profile_id is None:
        return None
    profile = await session.get(Profile, profile_id)
    if profile is None or profile.candidate_id != candidate_id:
        raise StudioInputError("profile_id does not belong to this candidate")
    return profile


async def resolve_base(
    session: AsyncSession,
    candidate_id: uuid.UUID,
    base_resume_id: uuid.UUID | None,
    profile: Profile | None,
) -> Resume:
    """The named résumé, else the profile's base, else the newest parsed version."""
    if base_resume_id is not None:
        row = await session.get(Resume, base_resume_id)
        if row is None or row.candidate_id != candidate_id:
            raise StudioInputError("base_resume_id does not belong to this candidate")
    elif profile is not None and profile.base_resume_id is not None:
        row = await session.get(Resume, profile.base_resume_id)
    else:
        row = await session.scalar(
            select(Resume)
            .where(Resume.candidate_id == candidate_id, Resume.parsed_json.is_not(None))
            .order_by(Resume.version.desc())
            .limit(1)
        )
    if row is None or not row.parsed_json:
        raise StudioInputError("upload a résumé first: the studio starts from one")
    return row


async def candidate_projects(session: AsyncSession, candidate_id: uuid.UUID) -> list[Project]:
    rows = await session.scalars(
        select(Project).where(Project.candidate_id == candidate_id).order_by(Project.name)
    )
    return list(rows.all())


async def studio_source(
    session: AsyncSession,
    candidate_id: uuid.UUID,
    base: ParsedResume,
    *,
    include_internships: bool,
    certification_ids: Sequence[uuid.UUID] | None,
) -> Source:
    internships, certifications = await confirmed_evidence(
        session,
        candidate_id,
        include_internships=include_internships,
        certification_ids=certification_ids,
    )
    return build_source(base, internships, certifications)
