"""API shapes for the setup and recovery page.

Separate from `schemas.py`, which is past this repo's size limit. Nothing in
here carries a secret: every field is a state, a count, a timestamp, or text
written by a diagnostic that already refuses to repeat a credential.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

SetupState = Literal["ok", "attention", "blocked", "unknown"]


class SetupItemOut(BaseModel):
    """One thing the installation needs, whether it has it, and how to get it."""

    key: str
    title: str
    #: `core`, `credentials`, `discovery` or `tools` — the page groups by it.
    group: str
    state: SetupState
    detail: str
    #: Commands or edits, in the order to try them. Empty when nothing is wrong.
    steps: list[str] = Field(default_factory=list)
    #: Small non-secret facts behind the verdict, for the owner to check it.
    facts: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    #: Actions the page can offer for this item, e.g. `registry_sync`.
    actions: list[str] = Field(default_factory=list)


class SetupStatusOut(BaseModel):
    generated_at: datetime
    #: The worst state of any item.
    overall: SetupState
    items: list[SetupItemOut]


class RegistrySyncIn(BaseModel):
    #: True by default: the page previews before it changes anything.
    dry_run: bool = True


class RegistrySyncOut(BaseModel):
    dry_run: bool
    summary: str
    created: int
    verified: int
    newer_in_db: int
    retired: int
    retired_newer_in_db: int
    moved_boards: list[str]


__all__ = [
    "RegistrySyncIn",
    "RegistrySyncOut",
    "SetupItemOut",
    "SetupState",
    "SetupStatusOut",
]
