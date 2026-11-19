"""What the API returns for graph questions (`packages/matching/graph_rag.py`)."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict


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
