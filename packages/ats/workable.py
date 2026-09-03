"""Workable adapter — CLAUDE.md §8, fourth in the build order.

Built from the live DOM of Open's Product Engineer posting and application:
``apply.workable.com/open-252/j/6D0FEF463C``. Workable differs from the other
adapters again:

- stable ``data-ui`` attributes identify posting content;
- the application has one real, unnamed ``form``;
- labels wrap controls three ancestors above them;
- file inputs have no name and a generated id;
- employer questions use opaque names such as ``QA_10337894``.

Only parsing and enumeration were verified without changing a real form.
Filling and submission remain explicit gaps rather than unchecked write paths.
"""

from __future__ import annotations

import re
from typing import Any

from packages.ats.base import (
    FillReport,
    ManualCompletionRequired,
    Option,
    ParsedPosting,
    Question,
    QuestionKind,
    Receipt,
    SiteError,
)
from packages.ats.greenhouse import _clean_label, _kind_for

#: https://apply.workable.com/<company>/j/<id>[/apply]
_URL_RE = re.compile(
    r"^https?://apply\.workable\.com/(?P<company>[\w.-]+)/j/(?P<job_id>[A-Z0-9]+)"
    r"(?:/apply)?/?(?:[?#].*)?$",
    re.I,
)

SELECTORS: dict[str, str] = {
    "form": "form[action*='/apply']",
    "posting_title": "[data-ui='job-title']",
    "posting_location": "[data-ui='job-workplace']",
    "posting_body": (
        "[data-ui='job-description'], [data-ui='job-requirements'], [data-ui='job-benefits']"
    ),
    "fields": (
        "input:not([type='hidden']):not([type='submit']):not([type='button'])"
        ":not([id^='iti-']), textarea, select"
    ),
    "label_ancestor": "xpath=ancestor::label[1]",
    "file_container": "xpath=ancestor::div[2]",
    "option": "option",
    "submit_button": "button[type='submit'], input[type='submit']",
    "confirmation": "[data-ui*='confirmation'], [class*='confirmation'], [class*='thank']",
    "captcha": (
        "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], "
        ".g-recaptcha, .h-captcha, iframe[title*='challenge']"
    ),
    "closed_marker": (
        "text=/no longer accepting applications/i, "
        "text=/this job is closed/i, text=/position has been filled/i"
    ),
}

_FILE_PROMPT = "Choose file or drag and drop here"
_EMPLOYER_PREFIX = "QA_"

_CORE_KEYS = {
    "firstname": "first_name",
    "lastname": "last_name",
    "email": "email",
    "phone": "phone",
    "address": "location",
    "city": "location",
    "postcode": "location",
    "country": "location",
    "resume": "resume",
    "cover_letter": "cover_letter",
}


def profile_key_for(field_name: str) -> str | None:
    """Return deterministic core mapping; opaque QA fields never guess."""
    return _CORE_KEYS.get(field_name)


def is_employer_question(field_name: str) -> bool:
    """Check if a field is an employer-specific question."""
    return field_name.startswith(_EMPLOYER_PREFIX)


class WorkableAdapter:
    """Read a Workable posting and its application questions."""

    name = "workable"

    @staticmethod
    def matches(url: str) -> bool:
        """Check if URL is a Workable posting."""
        return bool(_URL_RE.match(url))

    @staticmethod
    def company_slug(url: str) -> str | None:
        """Extract company slug from Workable URL."""
        match = _URL_RE.match(url)
        return match.group("company") if match else None

    @staticmethod
    def external_id(url: str) -> str | None:
        """Extract job ID from Workable URL."""
        match = _URL_RE.match(url)
        return match.group("job_id") if match else None

    async def _guard_automation_blocks(self, page: Any) -> None:
        """Stop on CAPTCHA; never attempt to bypass it. CLAUDE.md §2.5."""
        if await page.locator(SELECTORS["captcha"]).count():
            raise ManualCompletionRequired("captcha present; finish this application by hand")

    async def parse_posting(self, page: Any) -> ParsedPosting:
        """Parse posting details from a Workable page."""
        url = page.url
        closed = bool(await page.locator(SELECTORS["closed_marker"]).count())

        async def _first_text(selector: str) -> str | None:
            locator = page.locator(selector).first
            if not await locator.count():
                return None
            return _clean_label(await locator.inner_text()) or None

        body_nodes = page.locator(SELECTORS["posting_body"])
        body_parts = [
            _clean_label(await body_nodes.nth(index).inner_text())
            for index in range(await body_nodes.count())
        ]
        body = "\n\n".join(part for part in body_parts if part) or None

        return ParsedPosting(
            url=url,
            external_id=self.external_id(url),
            title=await _first_text(SELECTORS["posting_title"]),
            location=await _first_text(SELECTORS["posting_location"]),
            description_raw=body,
            closed=closed,
        )

    async def enumerate_fields(self, page: Any) -> list[Question]:
        """Walk the live form without changing any value."""
        await self._guard_automation_blocks(page)

        form = page.locator(SELECTORS["form"]).first
        if not await form.count():
            raise SiteError("no application form found on page")

        controls = form.locator(SELECTORS["fields"])
        questions: list[Question] = []
        seen_groups: set[str] = set()

        for index in range(await controls.count()):
            control = controls.nth(index)
            tag = (await control.evaluate("el => el.tagName")).lower()
            input_type = (await control.get_attribute("type") or "").lower()
            raw_key = await control.get_attribute("name") or await control.get_attribute("id")
            label = await self._label_for(control, raw_key or "")

            key = raw_key
            if input_type == "file":
                lowered = label.lower()
                if lowered.startswith("resume"):
                    key = "resume"
                elif lowered.startswith("photo"):
                    key = "photo"
            if not key:
                continue

            multiple = await control.get_attribute("multiple") is not None
            role = (await control.get_attribute("role") or "").lower()
            css = await control.get_attribute("class") or ""
            kind = _kind_for(tag, input_type, key, multiple, role, css)

            if kind in (QuestionKind.RADIO, QuestionKind.CHECKBOX):
                if key in seen_groups:
                    continue
                seen_groups.add(key)

            options: list[Option] = []
            if kind in (QuestionKind.SINGLE_SELECT, QuestionKind.MULTI_SELECT) and tag == "select":
                nodes = control.locator(SELECTORS["option"])
                for option_index in range(await nodes.count()):
                    node = nodes.nth(option_index)
                    value = await node.get_attribute("value") or ""
                    if value:
                        options.append(
                            Option(label=_clean_label(await node.inner_text()), value=value)
                        )

            required = (
                await control.get_attribute("required") is not None
                or (await control.get_attribute("aria-required")) == "true"
            )
            questions.append(
                Question(key=key, label=label, kind=kind, required=required, options=options)
            )

        return questions

    async def _label_for(self, control: Any, key: str) -> str:
        """Return employer-visible wording, never meaning inferred from QA id."""
        label = control.locator(SELECTORS["label_ancestor"]).first
        if await label.count():
            text = _clean_label(await label.inner_text())
            if text:
                return text

        if (await control.get_attribute("type") or "").lower() == "file":
            container = control.locator(SELECTORS["file_container"]).first
            if await container.count():
                text = _clean_label((await container.inner_text()).replace(_FILE_PROMPT, ""))
                if text:
                    return text

        if is_employer_question(key):
            return "(this question's wording could not be read from the page)"
        return key.replace("_", " ")

    async def fill(self, page: Any, answers: dict[str, Any]) -> FillReport:
        """Fill a Workable form (not implemented)."""
        raise NotImplementedError(
            "Workable fill is not implemented. Parsing and enumeration were verified "
            "against a live form; filling was not, so no unchecked values are written."
        )

    async def submit(self, page: Any) -> Receipt:
        """Submit a Workable application (not implemented)."""
        raise NotImplementedError("Workable submit is not implemented.")
