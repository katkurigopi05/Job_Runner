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
