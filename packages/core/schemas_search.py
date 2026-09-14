"""API shapes for saved search preferences.

See `models_search.py` for why they are not on the profile.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: The `/matches` query parameters a saved search may hold. Anything else is
#: refused rather than stored, so a saved search cannot quietly carry a
#: parameter the feed would ignore.
FILTER_KEYS = frozenset(
    {
        "keywords",
        "locations",
        "remote",
        "min_seniority",
        "max_seniority",
        "posted_within_days",
        "include_closed",
        "us_only",
        "remote_outside_california",
        "allow_unknown_location",
        "allow_unknown_seniority",
        "min_salary",
        "salary_currency",
        "salary_period",
        "include_unknown_salary",
        "wanted_skills",
        "lacking_skills",
        "include_unknown_skills",
        "max_education",
        "include_unknown_education",
    }
)


class SearchPreferenceIn(BaseModel):
    filters: dict[str, str] = Field(default_factory=dict)

    @field_validator("filters")
    @classmethod
    def _known_keys(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = sorted(set(value) - FILTER_KEYS)
        if unknown:
            raise ValueError(f"not a feed filter: {', '.join(unknown)}")
        if any(len(item) > 500 for item in value.values()):
            raise ValueError("a filter value is longer than 500 characters")
        return {key: item for key, item in value.items() if item != ""}


class SearchPreferenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scope: str
    name: str
    filters: dict[str, str] = Field(validation_alias="filters_json")
    updated_at: datetime


__all__ = ["FILTER_KEYS", "SearchPreferenceIn", "SearchPreferenceOut"]
