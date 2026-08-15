"""The ATS adapter contract — CLAUDE.md §8.

Everything ATS-specific lives behind this Protocol. Nothing above it (worker,
API, tailoring) may branch on which ATS it is talking to.

The two types that carry the product's safety guarantees:

- `FillReport.unanswered` holds the *exact* question text for anything the
  agent could not answer. That is what parks the application at `needs_review`
  instead of guessing (CLAUDE.md §2.4).
- `Receipt` is the field-by-field record plus a screenshot — the audit trail
  the owner reviews before anything is submitted.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class QuestionKind(StrEnum):
    """Taxonomy from the reference product's review schema. Do not extend
    casually — adapters and the answer mapper both switch on these."""

    TEXT = "text"
    TEXTAREA = "textarea"
    EMAIL = "email"
    PHONE = "phone"
    URL = "url"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"
    RADIO = "radio"
    CHECKBOX = "checkbox"
    BOOLEAN = "boolean"
    DATE = "date"
    FILE = "file"
    COVER_LETTER = "cover_letter"
    TYPEAHEAD = "typeahead"
    HIDDEN = "hidden"
    DISPLAY = "display"


class Option(BaseModel):
    """A choice on a select, radio group, or checkbox set."""

    label: str
    value: str


class Question(BaseModel):
    """One field on an application form, as the page actually presents it."""

    #: Stable handle for filling — usually the DOM id or name attribute.
    key: str
    #: The label a human sees, verbatim. Never paraphrased: this is what gets
    #: surfaced to the owner when the field cannot be answered.
    label: str
    kind: QuestionKind
    required: bool = False
    options: list[Option] = Field(default_factory=list)
    #: CSS selector the adapter will use to interact with the control.
    selector: str | None = None
    #: Helper/description text shown alongside the field, when present.
    help_text: str | None = None


class ParsedPosting(BaseModel):
    """What the adapter could read off the posting page."""

    external_id: str | None = None
    title: str | None = None
    company: str | None = None
    location: str | None = None
    description_raw: str | None = None
    #: True when the posting is gone or closed — maps to `job_closed`.
    closed: bool = False


class FilledField(BaseModel):
    key: str
    label: str
    kind: QuestionKind
    #: Redacted for file uploads and anything the owner marked sensitive.
    value: str | None = None


class SkippedField(BaseModel):
    key: str
    label: str
    reason: str


class UnansweredQuestion(BaseModel):
    """A question the agent refused to guess at.

    `question` is the site's exact wording. Preserving it verbatim is the
    whole point — a paraphrase is not something the owner can answer safely.
    """

    key: str
    question: str
    kind: QuestionKind
    options: list[Option] = Field(default_factory=list)
    required: bool = False


class FillReport(BaseModel):
    """Every field filled, every field skipped, every question left open."""

    filled: list[FilledField] = Field(default_factory=list)
    skipped: list[SkippedField] = Field(default_factory=list)
    unanswered: list[UnansweredQuestion] = Field(default_factory=list)
    #: Storage key of the filled-form screenshot, when one was taken.
    screenshot_ref: str | None = None

    @property
    def is_complete(self) -> bool:
        """True when nothing required was left open.

        A False here is what sends the application to `needs_review`.
        """
        return not any(q.required for q in self.unanswered)

    @property
    def fill_rate(self) -> float:
        """Share of encountered fields that got a value. Gate 2 tracks this."""
        total = len(self.filled) + len(self.skipped) + len(self.unanswered)
        return len(self.filled) / total if total else 0.0


class Receipt(BaseModel):
    """Proof of what was submitted, for the owner's audit."""

    submitted: bool
    ats: str
    url: str
    fields: list[FilledField] = Field(default_factory=list)
    screenshot_ref: str | None = None
    confirmation_text: str | None = None
    #: Anything the site echoed back — reference number, application id.
    site_reference: str | None = None


@runtime_checkable
class ATSAdapter(Protocol):
    """One implementation per ATS, one file per adapter."""

    name: str

    @staticmethod
    def matches(url: str) -> bool:
        """True if this adapter handles the given posting URL."""
        ...

    async def parse_posting(self, page: Any) -> ParsedPosting: ...

    async def enumerate_fields(self, page: Any) -> list[Question]: ...

    async def fill(self, page: Any, answers: dict[str, Any]) -> FillReport: ...

    async def submit(self, page: Any) -> Receipt: ...


class UnsupportedSiteError(Exception):
    """No adapter claims this URL — maps to `unsupported_site`."""


class SiteError(Exception):
    """The page did not behave as the adapter expects — maps to `site_error`.

    Message must not carry page HTML; it is written to the task row.
    """


class ManualCompletionRequired(Exception):
    """A captcha, login wall, or bot check stopped automation.

    Maps to `manual_completion_required`. CLAUDE.md §2.5 makes this a hard
    scope boundary: the owner finishes by hand, the agent does not evade.
    """
