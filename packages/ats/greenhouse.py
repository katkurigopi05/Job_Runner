"""Greenhouse adapter.

First adapter because it needs no login and has a stable DOM. Greenhouse
serves two generations of markup — the legacy `boards.greenhouse.io` form and
the newer `job-boards.greenhouse.io` one — so selectors are written as
comma-separated alternatives and fields are discovered by walking the form
rather than by assuming a fixed list. A posting with three custom questions and
one with none both work without an adapter change.

All selectors live in SELECTORS. A DOM change should be a one-place fix.
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from packages.ats.base import (
    FilledField,
    FillReport,
    ManualCompletionRequired,
    Option,
    ParsedPosting,
    Question,
    QuestionKind,
    Receipt,
    SiteError,
    SkippedField,
    UnansweredQuestion,
)

log = structlog.get_logger(__name__)

SELECTORS: dict[str, str] = {
    # The application form itself, both generations.
    "form": "#application_form, form#application-form, form[id*='application']",
    "posting_title": "h1.app-title, h1.section-header, .job__title h1, h1",
    "posting_location": ".location, .job__location, [class*='location']",
    "posting_body": "#content, .job__description, [class*='job-post-content']",
    # Any control inside the form that can hold a value.
    "fields": (
        "input:not([type='hidden']):not([type='submit']):not([type='button']), textarea, select"
    ),
    "field_container": "div, fieldset, li",
    "submit_button": (
        "#submit_app, input[type='submit'], button[type='submit'], "
        "button:has-text('Submit Application')"
    ),
    "confirmation": (
        "#application_confirmation, .application-confirmation, "
        "[class*='confirmation'], [class*='thank']"
    ),
    # Presence of any of these means we stop and hand back to the owner.
    "captcha": (
        "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], "
        ".g-recaptcha, .h-captcha, iframe[title*='challenge']"
    ),
    "closed_marker": (
        "text=/no longer accepting applications/i, "
        "text=/this job is closed/i, text=/position has been filled/i"
    ),
}

#: Greenhouse marks required fields with an asterisk in the label.
_REQUIRED_MARKERS = ("*", "(required)", "required")

#: `boards.greenhouse.io/acme/jobs/1234` or `job-boards.greenhouse.io/acme/jobs/1234`
_URL_RE = re.compile(
    r"^https?://(?:job-)?boards\.greenhouse\.io/(?P<company>[^/]+)/jobs/(?P<job_id>\d+)",
    re.IGNORECASE,
)
#: Embedded form on a company's own careers page.
_EMBED_RE = re.compile(r"greenhouse\.io/embed/job_app\?.*\bfor=(?P<company>[^&]+)", re.I)


def _looks_required(label: str, element_required: bool) -> bool:
    if element_required:
        return True
    lowered = label.lower()
    return any(marker in lowered for marker in _REQUIRED_MARKERS)


def _clean_label(raw: str) -> str:
    """Strip the required marker but keep the question's own wording intact."""
    text = " ".join(raw.split())
    for marker in ("*", "(required)"):
        text = text.replace(marker, "")
    return text.strip().rstrip(":").strip()


def _kind_for(tag: str, input_type: str, key: str, multiple: bool) -> QuestionKind:
    if tag == "textarea":
        return QuestionKind.COVER_LETTER if "cover" in key.lower() else QuestionKind.TEXTAREA
    if tag == "select":
        return QuestionKind.MULTI_SELECT if multiple else QuestionKind.SINGLE_SELECT
    return {
        "email": QuestionKind.EMAIL,
        "tel": QuestionKind.PHONE,
        "url": QuestionKind.URL,
        "file": QuestionKind.FILE,
        "date": QuestionKind.DATE,
        "checkbox": QuestionKind.CHECKBOX,
        "radio": QuestionKind.RADIO,
        "hidden": QuestionKind.HIDDEN,
    }.get(input_type, QuestionKind.TEXT)


class GreenhouseAdapter:
    """Drives a Greenhouse application form."""

    name = "greenhouse"

    @staticmethod
    def matches(url: str) -> bool:
        return bool(_URL_RE.match(url) or _EMBED_RE.search(url))

    @staticmethod
    def external_id(url: str) -> str | None:
        match = _URL_RE.match(url)
        return match.group("job_id") if match else None

    @staticmethod
    def company_slug(url: str) -> str | None:
        match = _URL_RE.match(url) or _EMBED_RE.search(url)
        return match.group("company") if match else None

    async def _guard_automation_blocks(self, page: Any) -> None:
        """Stop on a captcha rather than trying to get around it.

        CLAUDE.md §2.5 — this is a hard scope boundary, not a gap to close.
        """
        if await page.locator(SELECTORS["captcha"]).count():
            raise ManualCompletionRequired("captcha present; finish this application by hand")

    async def parse_posting(self, page: Any) -> ParsedPosting:
        url = page.url

        closed = False
        for marker in SELECTORS["closed_marker"].split(", "):
            if await page.locator(marker).count():
                closed = True
                break

        async def _first_text(selector: str) -> str | None:
            locator = page.locator(selector).first
            if await locator.count():
                text = await locator.inner_text()
                return " ".join(text.split()) or None
            return None

        body = None
        body_locator = page.locator(SELECTORS["posting_body"]).first
        if await body_locator.count():
            body = await body_locator.inner_text()

        return ParsedPosting(
            external_id=self.external_id(url),
            title=await _first_text(SELECTORS["posting_title"]),
            company=self.company_slug(url),
            location=await _first_text(SELECTORS["posting_location"]),
            description_raw=body,
            closed=closed,
        )

    async def enumerate_fields(self, page: Any) -> list[Question]:
        """Walk the real form. The field list comes from the page, not a guess."""
        await self._guard_automation_blocks(page)

        form = page.locator(SELECTORS["form"]).first
        if not await form.count():
            raise SiteError("no application form found on page")

        controls = form.locator(SELECTORS["fields"])
        count = await controls.count()

        questions: list[Question] = []
        seen_radio_groups: set[str] = set()

        for index in range(count):
            control = controls.nth(index)
            key = await control.get_attribute("id") or await control.get_attribute("name")
            if not key:
                continue

            tag = (await control.evaluate("el => el.tagName")).lower()
            input_type = (await control.get_attribute("type") or "").lower()
            multiple = await control.get_attribute("multiple") is not None
            kind = _kind_for(tag, input_type, key, multiple)

            # A radio group is one question, not one per option.
            if kind is QuestionKind.RADIO:
                group = await control.get_attribute("name") or key
                if group in seen_radio_groups:
                    continue
                seen_radio_groups.add(group)
                key = group

            label = await self._label_for(page, form, control, key)
            element_required = await control.get_attribute("required") is not None

            options: list[Option] = []
            if kind in (QuestionKind.SINGLE_SELECT, QuestionKind.MULTI_SELECT):
                option_nodes = control.locator("option")
                for opt_index in range(await option_nodes.count()):
                    node = option_nodes.nth(opt_index)
                    value = await node.get_attribute("value") or ""
                    text = _clean_label(await node.inner_text())
                    if value:  # skip the empty "Please select" placeholder
                        options.append(Option(label=text, value=value))

            questions.append(
                Question(
                    key=key,
                    label=label,
                    kind=kind,
                    required=_looks_required(label, element_required),
                    options=options,
                    selector=f"#{key}" if await control.get_attribute("id") else None,
                )
            )

        return questions

    async def _label_for(self, page: Any, form: Any, control: Any, key: str) -> str:
        """The label exactly as the site words it.

        Falls back through `for=`, an ancestor label, then aria-label. Returns
        the key only as a last resort — a wrong label would be shown to the
        owner as if the employer had asked it.
        """
        control_id = await control.get_attribute("id")
        if control_id:
            escaped = control_id.replace('"', '\\"')
            label = form.locator(f'label[for="{escaped}"]').first
            if await label.count():
                return _clean_label(await label.inner_text())

        ancestor = control.locator("xpath=ancestor::label[1]")
        if await ancestor.count():
            return _clean_label(await ancestor.first.inner_text())

        aria = await control.get_attribute("aria-label")
        if aria:
            return _clean_label(aria)

        return key

    async def fill(self, page: Any, answers: dict[str, Any]) -> FillReport:
        """Fill what we have answers for. Never invent one.

        A question with no answer goes into `unanswered` carrying its exact
        text, which is what parks the application for the owner.
        """
        await self._guard_automation_blocks(page)

        questions = await self.enumerate_fields(page)
        report = FillReport()

        for question in questions:
            if question.kind in (QuestionKind.HIDDEN, QuestionKind.DISPLAY):
                continue

            if question.key not in answers or answers[question.key] in (None, ""):
                if question.required:
                    report.unanswered.append(
                        UnansweredQuestion(
                            key=question.key,
                            question=question.label,
                            kind=question.kind,
                            options=question.options,
                            required=True,
                        )
                    )
                else:
                    report.skipped.append(
                        SkippedField(
                            key=question.key,
                            label=question.label,
                            reason="no answer in profile and field is optional",
                        )
                    )
                continue

            value = answers[question.key]
            try:
                await self._set_value(page, question, value)
            except ManualCompletionRequired:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad field must not abort
                log.warning(
                    "field_fill_failed",
                    key=question.key,
                    kind=question.kind.value,
                    error=type(exc).__name__,
                )
                report.skipped.append(
                    SkippedField(
                        key=question.key,
                        label=question.label,
                        reason=f"could not fill: {type(exc).__name__}",
                    )
                )
                continue

            report.filled.append(
                FilledField(
                    key=question.key,
                    label=question.label,
                    kind=question.kind,
                    # File contents are never echoed into the report.
                    value=None if question.kind is QuestionKind.FILE else str(value),
                )
            )

        return report

    async def _set_value(self, page: Any, question: Question, value: Any) -> None:
        selector = question.selector or f"#{question.key}"
        locator = page.locator(selector).first

        match question.kind:
            case QuestionKind.FILE:
                await locator.set_input_files(str(value))
            case QuestionKind.SINGLE_SELECT | QuestionKind.MULTI_SELECT:
                await locator.select_option(str(value))
            case QuestionKind.CHECKBOX | QuestionKind.BOOLEAN:
                if bool(value):
                    await locator.check()
                else:
                    await locator.uncheck()
            case QuestionKind.RADIO:
                escaped = str(value).replace('"', '\\"')
                await page.locator(f'input[name="{question.key}"][value="{escaped}"]').first.check()
            case _:
                await locator.fill(str(value))

    async def submit(self, page: Any) -> Receipt:
        """Click submit and capture what the site says back.

        Only ever reached after the approval gate — see apps/worker/apply_job.py.
        """
        await self._guard_automation_blocks(page)

        button = page.locator(SELECTORS["submit_button"]).first
        if not await button.count():
            raise SiteError("no submit button found on application form")

        await button.click()
        await page.wait_for_load_state("networkidle")

        confirmation = None
        confirm_locator = page.locator(SELECTORS["confirmation"]).first
        if await confirm_locator.count():
            confirmation = " ".join((await confirm_locator.inner_text()).split())

        return Receipt(
            submitted=True,
            ats=self.name,
            url=page.url,
            confirmation_text=confirmation,
        )
