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

    @staticmethod
    def application_url(url: str) -> str:
        """Where the application form for this posting lives.

        Greenhouse answers with the posting URL itself; the other three put the
        form on a route of its own. Returning the input unchanged is always
        valid — an adapter never invents a route for a URL it cannot read.
        """
        ...

    @staticmethod
    def profile_key_for(field_name: str) -> str | None:
        """The profile key this field takes its answer from, or None.

        The ATS's own statement about its field names — `_systemfield_name` is
        the full name, `urls[LinkedIn]` is the LinkedIn URL — which is better
        evidence than a regex over whatever label sits beside it. None means
        only a human knows, and §2.4 parks the question rather than guessing.

        Greenhouse answers None throughout: its field names are already the
        words a label rule matches.
        """
        ...

    async def wait_for_form(self, page: Any, timeout_ms: int = ...) -> None:
        """Block until the employer's questions have rendered.

        Three of the four ATSes render the form with React after
        `domcontentloaded`, so a check that runs immediately sees an empty
        page and reports a missing form that is merely late.
        """
        ...

    async def wait_for_posting(self, page: Any, timeout_ms: int = ...) -> None:
        """Give the posting a chance to render before it is read.

        Best-effort: a withdrawn posting has no title, and raising here would
        report `site_error` for the one outcome that is expected and permanent.
        """
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


class PostingGone(Exception):
    """The posting has been taken down — maps to `job_closed`.

    Separate from `UnsupportedSiteError` because a redirect off the ATS means
    one of two different things and they have opposite consequences for the
    registry. Greenhouse answers 200 for both:

    - Stripe's board redirects *every* posting, live ones included, to
      `stripe.com/careers/listing/<role>/<id>`. The employer hosts the
      application themselves, and we have no extractor for a bespoke page.
    - A withdrawn Cloudflare posting redirects to
      `cloudflare.com/careers/#open-roles` — the careers index, naming no job.
      Cloudflare's board is fine; this one role is gone.

    The landing URL still carrying the job id is what tells them apart.
    """


class ManualCompletionRequired(Exception):
    """A captcha, login wall, or bot check stopped automation.

    Maps to `manual_completion_required`. CLAUDE.md §2.5 makes this a hard
    scope boundary: the owner finishes by hand, the agent does not evade.
    """
