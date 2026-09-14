"""API shapes for contacts, tasks and the calendar export."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from packages.core.models_tracking import RELATIONSHIPS, TASK_KINDS


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise ValueError("include a time zone, e.g. 2026-09-20T15:00:00-07:00")
    return value


class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=50)
    company: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=200)
    profile_url: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=5000)

    @field_validator("profile_url")
    @classmethod
    def _http_only(cls, value: str | None) -> str | None:
        # Rendered as a link on the dashboard, so `javascript:` must not store.
        if value and urlparse(value).scheme not in ("http", "https"):
            raise ValueError("profile_url must be an http or https link")
        return value or None


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str | None
    phone: str | None
    company: str | None
    role: str | None
    profile_url: str | None
    notes: str | None
    updated_at: datetime


class LinkContactIn(BaseModel):
    """Link an existing contact, or create one and link it."""

    contact_id: uuid.UUID | None = None
    contact: ContactIn | None = None
    relationship: str = "recruiter"

    @field_validator("relationship")
    @classmethod
    def _known(cls, value: str) -> str:
        if value not in RELATIONSHIPS:
            raise ValueError(f"relationship must be one of {', '.join(RELATIONSHIPS)}")
        return value

    @model_validator(mode="after")
    def _one_source(self) -> LinkContactIn:
        if (self.contact_id is None) == (self.contact is None):
            raise ValueError("give exactly one of contact_id or contact")
        return self


class LinkedContactOut(BaseModel):
    relationship: str
    contact: ContactOut


class ChecklistItem(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    done: bool = False


class TaskIn(BaseModel):
    kind: str
    title: str = Field(min_length=1, max_length=300)
    due_at: datetime | None = None
    reminder_at: datetime | None = None
    location: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=5000)
    #: Omitted means the default checklist for the kind.
    checklist: list[str] | None = None

    _due = field_validator("due_at")(classmethod(lambda cls, v: _aware(v)))
    _reminder = field_validator("reminder_at")(classmethod(lambda cls, v: _aware(v)))

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if value not in TASK_KINDS:
            raise ValueError(f"kind must be one of {', '.join(TASK_KINDS)}")
        return value

    @model_validator(mode="after")
    def _order(self) -> TaskIn:
        if self.due_at and self.reminder_at and self.reminder_at > self.due_at:
            raise ValueError("reminder_at must not be after due_at")
        return self


class TaskPatch(BaseModel):
    """Only the fields sent change. Send `due_at: null` to clear a date."""

    title: str | None = Field(default=None, min_length=1, max_length=300)
    due_at: datetime | None = None
    reminder_at: datetime | None = None
    location: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=5000)
    checklist: list[ChecklistItem] | None = None
    completed: bool | None = None

    _due = field_validator("due_at")(classmethod(lambda cls, v: _aware(v)))
    _reminder = field_validator("reminder_at")(classmethod(lambda cls, v: _aware(v)))


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    application_id: uuid.UUID
    kind: str
    title: str
    due_at: datetime | None
    reminder_at: datetime | None
    reminded_at: datetime | None
    completed_at: datetime | None
    location: str | None
    notes: str | None
    checklist: list[dict[str, Any]] = Field(validation_alias="checklist_json")
    source: str
    created_at: datetime


class UpcomingTaskOut(TaskOut):
    application_url: str
    overdue: bool


class TrackingOut(BaseModel):
    contacts: list[LinkedContactOut]
    tasks: list[TaskOut]


__all__ = [
    "ChecklistItem",
    "ContactIn",
    "ContactOut",
    "LinkContactIn",
    "LinkedContactOut",
    "TaskIn",
    "TaskOut",
    "TaskPatch",
    "TrackingOut",
    "UpcomingTaskOut",
]
