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
    FillReport,
    ManualCompletionRequired,
    Option,
    ParsedPosting,
    Question,
    QuestionKind,
    Receipt,
)
from packages.ats.form import field_selector, fill_form, submit_form
from packages.ats.navigate import (
    FORM_READY_TIMEOUT_MS,
    POSTING_READY_TIMEOUT_MS,
    application_route,
    locate_form,
    longest_text,
)
from packages.ats.navigate import wait_for_form as _wait_for_form
from packages.ats.navigate import wait_for_posting as _wait_for_posting

log = structlog.get_logger(__name__)

#: How long to wait for a react-select menu to render after opening it.
MENU_OPEN_TIMEOUT_MS = 3_000

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
    "react_select_listbox": '[id^="react-select-"][id$="-listbox"]',
    "react_select_option": '[role="option"]',
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

#: Greenhouse serves the form on the posting page itself — there is no
#: second route, and navigating to one would leave the only page that
#: carries the form.
APPLICATION_SEGMENT: str | None = None

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


#: Inputs that belong to a third-party widget rather than to the employer's
#: form. intl-tel-input mounts a country search box inside the phone field;
#: enumerating it offers the owner a question no employer asked.
_WIDGET_INTERNAL_KEYS = ("iti-", "react-select-")


def _is_widget_internal(key: str) -> bool:
    return key.startswith(_WIDGET_INTERNAL_KEYS)


def _kind_for(
    tag: str, input_type: str, key: str, multiple: bool, role: str = "", css: str = ""
) -> QuestionKind:
    if tag == "textarea":
        return QuestionKind.COVER_LETTER if "cover" in key.lower() else QuestionKind.TEXTAREA
    if tag == "select":
        return QuestionKind.MULTI_SELECT if multiple else QuestionKind.SINGLE_SELECT
    # Greenhouse renders every dropdown with react-select, which is an
    # `input type="text"` carrying role="combobox" — there is no <select> on
    # the page at all. Trusting the type attribute classified work
    # authorization as free text, and §2.2 requires that answer to match an
    # offered option exactly. Typing into a combobox does not select anything,
    # so the answer silently never lands.
    if role == "combobox" or "select__input" in css:
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

    @staticmethod
    def profile_key_for(field_name: str) -> str | None:
        """Greenhouse has no key map, and does not need one.

        Its field names are already the words a label rule matches —
        `first_name`, `email`, `resume` — so there is nothing here that
        reading the label does not already answer. Present so every adapter
        answers the same question rather than the caller checking which have
        one.
        """
        return None

    @staticmethod
    def application_url(url: str) -> str:
        return application_route(url, _URL_RE, APPLICATION_SEGMENT)

    async def wait_for_posting(self, page: Any, timeout_ms: int = POSTING_READY_TIMEOUT_MS) -> None:
        """Give the posting a chance to render before reading it."""
        await _wait_for_posting(
            page, title_selector=SELECTORS["posting_title"], timeout_ms=timeout_ms
        )

    async def wait_for_form(self, page: Any, timeout_ms: int = FORM_READY_TIMEOUT_MS) -> None:
        """Block until the employer's questions are actually on the page."""
        await _wait_for_form(
            page,
            form_selector=SELECTORS["form"],
            field_selector=SELECTORS["fields"],
            captcha_selector=SELECTORS["captcha"],
            timeout_ms=timeout_ms,
        )

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

        body = await longest_text(page, SELECTORS["posting_body"])

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

        form = await locate_form(
            page,
            form_selector=SELECTORS["form"],
            field_selector=SELECTORS["fields"],
        )

        controls = form.locator(SELECTORS["fields"])
        count = await controls.count()

        questions: list[Question] = []
        seen_radio_groups: set[str] = set()

        for index in range(count):
            control = controls.nth(index)
            key = await control.get_attribute("id") or await control.get_attribute("name")
            if not key or _is_widget_internal(key):
                continue

            tag = (await control.evaluate("el => el.tagName")).lower()
            input_type = (await control.get_attribute("type") or "").lower()
            multiple = await control.get_attribute("multiple") is not None
            role = (await control.get_attribute("role") or "").lower()
            css = await control.get_attribute("class") or ""
            kind = _kind_for(tag, input_type, key, multiple, role, css)

            # A radio group is one question, not one per option.
            if kind is QuestionKind.RADIO:
                group = await control.get_attribute("name") or key
                if group in seen_radio_groups:
                    continue
                seen_radio_groups.add(group)
                key = group

            label = await self._label_for(page, form, control, key)
            # A radio group is addressed by the shared name, not by the id
            # of whichever option happened to be walked first.
            selector = (
                field_selector(None, key)
                if kind is QuestionKind.RADIO
                else field_selector(
                    await control.get_attribute("id"), await control.get_attribute("name")
                )
            )
            element_required = await control.get_attribute("required") is not None

            options: list[Option] = []
            if kind in (QuestionKind.SINGLE_SELECT, QuestionKind.MULTI_SELECT):
                if tag == "select":
                    option_nodes = control.locator("option")
                    for opt_index in range(await option_nodes.count()):
                        node = option_nodes.nth(opt_index)
                        value = await node.get_attribute("value") or ""
                        text = _clean_label(await node.inner_text())
                        if value:  # skip the empty "Please select" placeholder
                            options.append(Option(label=text, value=value))
                else:
                    options = await self._react_select_options(page, control)

            questions.append(
                Question(
                    key=key,
                    label=label,
                    kind=kind,
                    required=_looks_required(label, element_required),
                    options=options,
                    selector=selector,
                )
            )

        return questions

    async def _react_select_options(self, page: Any, control: Any) -> list[Option]:
        """Open react-select long enough to read its portal-rendered options."""
        # Opening a real form control is interaction. Stop before touching it
        # when an automation block is present; never try to route around one.
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        await self._guard_automation_blocks(page)
        await control.click()

        try:
            menu = page.locator(SELECTORS["react_select_listbox"]).first
            try:
                # react-select does not render inside the click handler — the
                # menu arrives a tick or more later, through React's scheduler.
                # count() does not wait, so checking immediately finds nothing
                # and reports a dropdown with no options, which reads
                # downstream as "the employer offered no choices" rather than
                # as a timing failure.
                await menu.wait_for(state="attached", timeout=MENU_OPEN_TIMEOUT_MS)
            except PlaywrightTimeout:
                # Genuinely no menu: a combobox that is disabled, empty, or not
                # react-select after all. An empty option list is the honest
                # answer, and §2.4 parks the question for the owner.
                return []

            nodes = menu.locator(SELECTORS["react_select_option"])
            options: list[Option] = []
            for index in range(await nodes.count()):
                node = nodes.nth(index)
                label = _clean_label(await node.inner_text())
                if label:
                    value = await node.get_attribute("data-value") or label
                    options.append(Option(label=label, value=value))
            return options
        finally:
            # Escape closes the menu without selecting an option or submitting.
            await control.press("Escape")

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
        """Fill what we have answers for. Never invent one."""
        return await fill_form(self, page, answers, selectors=SELECTORS)

    async def submit(self, page: Any) -> Receipt:
        """Click submit and capture what the site says back."""
        return await submit_form(self, page, selectors=SELECTORS)
