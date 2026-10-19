"""API shapes for grading the assistant's search (`/judgments`, Phase 5)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class JudgmentCandidateOut(BaseModel):
    """One posting to grade against one question.

    Which search mode found it, and at what rank, is left out on purpose: a
    grade given knowing which mode's answer it is leans toward that mode.
    """

    id: uuid.UUID
    set_version: str
    question_key: str
    question: str
    #: This posting's place among its question's, and how many there are.
    position: int
    of: int
    posting_id: uuid.UUID
    title: str | None = None
    company: str | None = None
    location: str | None = None
    url: str
    description: str | None = None
    #: The posting's text has changed since it was pooled. The grade is of
    #: what is shown now.
    changed: bool = False
    closed: bool = False


class JudgmentIn(BaseModel):
    """0 not what was asked, 1 partly, 2 what was asked for."""

    relevance: int = Field(ge=0, le=2)
    note: str | None = Field(default=None, max_length=1000)


class JudgmentOut(BaseModel):
    id: uuid.UUID
    question_key: str
    posting_id: uuid.UUID
    relevance: int | None
    note: str | None
    graded_at: datetime | None


class JudgmentSplitOut(BaseModel):
    total: int = 0
    graded: int = 0


class JudgmentSummaryOut(BaseModel):
    """How far the grading has gone. Dev is served first, held out after."""

    set_version: str | None
    total: int = 0
    graded: int = 0
    remaining: int = 0
    #: grade -> count.
    by_grade: dict[int, int] = Field(default_factory=dict)
    dev: JudgmentSplitOut = Field(default_factory=JudgmentSplitOut)
    held_out: JudgmentSplitOut = Field(default_factory=JudgmentSplitOut)
    questions: int = 0
    questions_done: int = 0
    notes: list[str] = Field(default_factory=list)
