"""What the API returns for graph questions (`packages/matching/graph_rag.py`)."""

from __future__ import annotations

import re
import uuid
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.github.code_signals import Classification


class ProjectJobOut(BaseModel):
    """One open job that directly requires a technology a project declares."""

    model_config = ConfigDict(extra="forbid")

    posting_id: uuid.UUID
    title: str
    company: str | None
    location: str | None
    url: str
    #: The shared technologies, the public projects that declare them, and the
    #: job's own requirement line for each: source facts, no model.
    evidence: str


class ProjectJobsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: open_postings: every linked open posting in the search area;
    #: top_matches: the owner's 100 best matches (before `make skill-links`).
    scope: Literal["open_postings", "top_matches"]
    #: Postings read to find these.
    searched: int
    summary: str
    note: str
    #: The best five, most shared technologies first.
    jobs: list[ProjectJobOut]


#: Jobs a gap's drill-down carries in full with the answer. The rest of the N are
#: carried as ids and read on request (`POST /chat/gap-jobs`), so a saved
#: conversation does not hold hundreds of quotes per gap.
GAP_JOBS_INLINE = 10
#: Jobs one `POST /chat/gap-jobs` reads.
GAP_JOBS_PAGE = 50


class GapJobOut(BaseModel):
    """One job behind a recurring-gap count: it directly requires the skill."""

    model_config = ConfigDict(extra="forbid")

    posting_id: uuid.UUID
    title: str
    company: str | None
    location: str | None
    url: str
    #: The job's own direct-requirement line for the skill. None only when read
    #: again later and the job no longer states one.
    quote: str | None
    #: as_answered: read when the answer was written. Read again on request:
    #: current (still open, still a direct requirement), closed, or changed
    #: (its text or saved requirements no longer require the skill directly).
    state: Literal["as_answered", "current", "closed", "changed"] = "as_answered"


class CodeCitationOut(BaseModel):
    """A file and lines that implement the skill, at a commit a Resume Studio save checked.

    From `kept_findings.py`: the repository's code, never a statement about who
    wrote it.
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    commit_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    #: The lines on github.com at that commit.
    url: str
    #: The static rule's own sentence for what it found.
    explanation: str

    @model_validator(mode="after")
    def _pinned(self) -> Self:
        if self.end_line < self.start_line:
            raise ValueError("a line range ends after it starts")
        if not re.match(rf"^https://github\.com/[^/]+/[^/]+/blob/{self.commit_sha}/", self.url):
            raise ValueError("a citation links to its file at its commit")
        return self


class GapProjectOut(BaseModel):
    """An eligible public project whose repository declares or implements the skill.

    Repository evidence, never a statement about the owner's own work: a
    claim of personal contribution is confirmed by the owner in Resume Studio
    (`StudioCodeApproval.contribution_confirmed`), not here.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: uuid.UUID
    #: owner/name when known, else the project's name.
    repository: str
    #: The repository, at the commit read when a live check ran for this answer.
    url: str
    #: Resume Studio's evidence categories: "declared" from the primary language
    #: or a dependency file; "implemented" only with the files a Studio save
    #: verified at a commit (`implemented_in`). Never documented or insufficient.
    category: Classification
    declared_in: list[Literal["dependency_file", "primary_language"]]
    #: The commit a live GitHub check read the declaration at; None for imported metadata.
    commit_sha: str | None = None
    #: Files that implement the skill, kept from Resume Studio saves. Empty for a
    #: declaration, and on answers written before they were kept.
    implemented_in: list[CodeCitationOut] = Field(default_factory=list)

    @model_validator(mode="after")
    def _evidence_matches_category(self) -> Self:
        # A declaration alone is never implementation evidence, and
        # implementation is never claimed without a commit-pinned file citation.
        if self.category is Classification.IMPLEMENTED:
            if not self.implemented_in:
                raise ValueError("implementation needs a commit-pinned file citation")
        elif self.category is Classification.DECLARED:
            if self.implemented_in:
                raise ValueError("a declared project carries no implementation citation")
            if not self.declared_in:
                raise ValueError("a declaration names where the repository declares the skill")
        else:
            raise ValueError("chat gap evidence is declared or implemented only")
        return self


class GapEvidenceOut(BaseModel):
    """The jobs and projects behind one recurring skill gap (`gap_evidence.py`)."""

    model_config = ConfigDict(extra="forbid")

    skill: str
    label: str
    #: The source the answer cites for this gap, e.g. "P1".
    source_label: str
    #: N: distinct jobs of the reviewed population that directly require the skill.
    total: int
    #: The population the count was taken over: every job the answer reviewed.
    reviewed: int
    #: All N, in the order the population was read. Fixed when the answer was
    #: written, so later pages list the same jobs the count was taken over.
    posting_ids: list[uuid.UUID]
    #: The first `GAP_JOBS_INLINE` of them, as the answer read them.
    jobs: list[GapJobOut]
    projects: list[GapProjectOut]

    @model_validator(mode="after")
    def _reconciles(self) -> Self:
        if len(self.posting_ids) != self.total or len(set(self.posting_ids)) != self.total:
            raise ValueError("a gap's count is its distinct jobs")
        if self.total > self.reviewed:
            raise ValueError("a gap cannot count more jobs than were reviewed")
        if [job.posting_id for job in self.jobs] != self.posting_ids[: len(self.jobs)]:
            raise ValueError("the jobs shown are the first of the jobs counted")
        return self


class GapJobsRequest(BaseModel):
    """More of a gap's jobs: ids from `GapEvidenceOut.posting_ids`, read again now."""

    model_config = ConfigDict(extra="forbid")

    skill: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    posting_ids: list[uuid.UUID] = Field(min_length=1, max_length=GAP_JOBS_PAGE)


class GapJobsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str
    #: In the order asked for; a posting that no longer exists is left out.
    jobs: list[GapJobOut]
