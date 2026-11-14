"""Request and response shapes for the Resume Studio and the evidence it reads.

`packages/tailor/studio.py` is what these describe. Internships and
certifications are the owner's statements, so every field is bounded and
nothing here has a default that would read as a claim.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from packages.core.schemas import ResumeContactEdit, ResumeOut

RoleTrackName = Literal["swe", "fullstack", "data_ml", "ai_agentic", "cloud_devops", "fde_harness"]

Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
Bullet = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]
Line = Annotated[str, StringConstraints(max_length=600)]
Optional40 = Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None


class InternshipIn(BaseModel):
    company: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    role: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    location: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    #: As the owner writes it ("Jun 2025"). Printed, never computed with.
    started: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
    ended: Optional40 = None
    stack: list[Short] = Field(default_factory=list, max_length=30)
    bullets: list[Bullet] = Field(default_factory=list, max_length=12)
    #: The owner confirmed it. Only a confirmed record reaches a résumé.
    verified: bool = False


class InternshipOut(InternshipIn):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID


class CertificationIn(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    issuer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    issued: Optional40 = None
    expires: Optional40 = None
    credential_id: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None
    ) = None
    skills: list[Short] = Field(default_factory=list, max_length=30)
    verified: bool = False


class CertificationOut(CertificationIn):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID


class EvidenceSave(BaseModel):
    """The whole list of each. What is not sent is removed."""

    internships: list[InternshipIn] = Field(default_factory=list, max_length=20)
    certifications: list[CertificationIn] = Field(default_factory=list, max_length=30)


class EvidenceOut(BaseModel):
    candidate_id: uuid.UUID
    internships: list[InternshipOut]
    certifications: list[CertificationOut]


class StudioRequest(BaseModel):
    candidate_id: uuid.UUID
    profile_id: uuid.UUID | None = None
    #: Defaults to the profile's base résumé, then the newest version.
    base_resume_id: uuid.UUID | None = None
    job_description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=25_000)
    ]
    role_track: RoleTrackName = "swe"
    #: The owner's pick, in order. None ranks the eligible ones.
    selected_project_ids: list[uuid.UUID] | None = Field(default=None, max_length=8)
    #: None means every confirmed certification; an empty list means none.
    selected_certification_ids: list[uuid.UUID] | None = Field(default=None, max_length=30)
    include_internships: bool = True
    project_limit: int = Field(default=4, ge=1, le=8)
    #: Ask the tailoring model to revise the bullets that can gain a term.
    #: Off by default: it sends those bullets to the provider set for
    #: tailoring (CLAUDE.md §2.8).
    use_model: bool = False


class StudioTarget(BaseModel):
    term: str
    evidence: str
    #: `bullet`, `practice` or `repository`.
    kind: str


class StudioBullet(BaseModel):
    ref: str
    original: str
    tailored: str
    status: str
    reason: str | None = None
    targets: list[StudioTarget] = Field(default_factory=list)


class StudioLine(BaseModel):
    text: str
    #: `resume`, `internship`, `certification` or `rewrite`.
    origin: str
    label: str = ""
    #: The line it replaced, for a rewrite.
    original: str | None = None


class StudioTerm(BaseModel):
    term: str
    status: str
    sources: list[str] = Field(default_factory=list)


class StudioProject(BaseModel):
    id: uuid.UUID
    name: str
    is_private: bool
    score: float
    matched_terms: list[str]
    chosen: bool
    held_back: str = ""


class StudioAts(BaseModel):
    parse: float
    keywords: float
    overall: float
    supported: list[str]
    missing: list[str]
    findings: list[str]


class StudioPreview(BaseModel):
    role_track: RoleTrackName
    role_label: str
    base_resume_id: uuid.UUID
    base_version: int
    contact: ResumeContactEdit
    sections: dict[str, list[str]]
    lines: dict[str, list[StudioLine]]
    html: str
    bullets: list[StudioBullet]
    terms: list[StudioTerm]
    projects: list[StudioProject]
    ats_before: StudioAts
    ats_after: StudioAts
    added: list[str]
    skipped: list[str]
    tailored_by: str | None = None
    calls: int = 0


class StudioRewrite(BaseModel):
    original: Line
    tailored: Line


class StudioSave(BaseModel):
    """A previewed draft, possibly edited, to keep as a new résumé version."""

    candidate_id: uuid.UUID
    profile_id: uuid.UUID | None = None
    base_resume_id: uuid.UUID
    role_track: RoleTrackName = "swe"
    selected_project_ids: list[uuid.UUID] = Field(default_factory=list, max_length=8)
    selected_certification_ids: list[uuid.UUID] | None = Field(default=None, max_length=30)
    include_internships: bool = True
    contact: ResumeContactEdit
    sections: dict[str, list[Line]]
    #: The rewrites the preview returned. Each is judged again before saving.
    rewrites: list[StudioRewrite] = Field(default_factory=list, max_length=100)
    #: Lines the owner typed on the studio screen. Theirs, so not judged.
    owner_edits: list[Line] = Field(default_factory=list, max_length=200)
    tailored_by: Annotated[str, StringConstraints(max_length=120)] | None = None
    #: Make it the profile's base résumé.
    adopt: bool = False


class StudioSaved(BaseModel):
    resume: ResumeOut
    adopted: bool
