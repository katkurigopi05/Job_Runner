"""API shapes for explicit ranking preferences and their suggestions."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RankingPreferenceIn(BaseModel):
    kind: str
    value: str = Field(min_length=1, max_length=200)
    #: Bounded like the column. Checked here so the error names the field.
    weight: float = Field(ge=-0.3, le=0.3)
    source: str = "explicit"
    note: str | None = Field(default=None, max_length=500)


class RankingPreferenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scope: str
    kind: str
    value: str
    weight: float
    source: str
    note: str | None
    updated_at: datetime


class SuggestionOut(BaseModel):
    kind: str
    value: str
    weight: float
    #: The decisions behind it, in words.
    evidence: str


__all__ = ["RankingPreferenceIn", "RankingPreferenceOut", "SuggestionOut"]


class RankingEvaluationOut(BaseModel):
    """Whether the personalized order ranks better on held-out owner grades.

    `learned_model` is always a statement that there is none: personalization
    here is explicit adjustments, and `promotable` is true only when the
    held-out interval clears the base one on enough grades from enough streams.
    """

    owner_labels: int
    streams: dict[str, int]
    held_out: int
    preferences: int
    status: str
    message: str
    k: int
    learned_model: str
    base_ndcg: float | None
    base_interval: list[float] | None
    personalized_ndcg: float | None
    personalized_interval: list[float] | None
    promotable: bool
    blockers: list[str]
