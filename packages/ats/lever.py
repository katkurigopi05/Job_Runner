"""Lever adapter — CLAUDE.md §8, second in the build order.

Built against the live DOM of a real Lever posting rather than a fixture
written from memory. That order matters: the Greenhouse adapter classified
every dropdown as free text for weeks because its fixture was hand-written and
the real board had moved to react-select.

What Lever actually does, as of 2026-08:

- One form, `#application-form`. Native `<select>` elements, not comboboxes —
  simpler than Greenhouse, and the shared field-kind logic already handles them.
- Core fields are plainly named: `name`, `email`, `phone`, `org`, `location`,
  `resume`.
- Links are bracketed: `urls[LinkedIn]`, `urls[GitHub]`, `urls[Portfolio]`.
- Everything employer-specific is `cards[<uuid>][...]`, which carries no meaning
  in the name at all. The label is the only description of what is being asked,
  which is exactly the case §2.4 is about: the question goes to the owner in the
  employer's words because nothing else identifies it.
- Checkbox and radio groups repeat one name across options, so they are one
  question, not one per option.
"""

from __future__ import annotations

import re
from typing import Any

from packages.ats.base import (
    FillReport,
    ManualCompletionRequired,
    ParsedPosting,
    Question,
    QuestionKind,
    Receipt,
    SiteError,
)
from packages.ats.greenhouse import _clean_label, _kind_for

#: https://jobs.lever.co/<company>/<posting-uuid>[/apply]
_URL_RE = re.compile(
    r"^https?://jobs\.(?:eu\.)?lever\.co/(?P<company>[\w.-]+)/(?P<job_id>[0-9a-f-]{16,})",
    re.I,
)

SELECTORS: dict[str, str] = {
    "form": "#application-form, form[id*='application']",
    "posting_title": ".posting-header h2, .posting-headline h2, h2",
    "posting_location": ".location, .posting-categories .location",
    "posting_body": ".posting-description, .section-wrapper .section, [data-qa='job-description']",
    "fields": (
        "input:not([type='hidden']):not([type='submit']):not([type='button']), textarea, select"
    ),
    "field_container": "li, .application-question, .application-field, div",
    "label": ".application-label, .application-question label, label, .text",
    "submit_button": "button[type='submit'], input[type='submit'], .template-btn-submit",
    "confirmation": ".application-confirmation, [class*='confirmation'], [class*='thank']",
    # Lever posts a captcha on its application forms too. §2.5 — stop, never
    # route around it.
    "captcha": (
        "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], "
        ".g-recaptcha, .h-captcha, iframe[title*='challenge']"
    ),
    "closed_marker": (
        "text=/no longer accepting applications/i, "
        "text=/this posting is closed/i, text=/position has been filled/i"
    ),
}

#: Lever's own names for the fields every posting has. Anything else is a
#: `cards[...]` question, which only its label describes.
_CORE_KEYS = {
    "name": "full_name",
    "email": "email",
    "phone": "phone",
    "org": "current_company",
    "location": "location",
    "resume": "resume",
    "urls[LinkedIn]": "linkedin",
    "urls[GitHub]": "github",
    "urls[Portfolio]": "portfolio",
    "urls[Other]": "website",
}


def profile_key_for(field_name: str) -> str | None:
    """The profile key a Lever field maps to, or None when only a human knows.

    A `cards[...]` field has a uuid for a name and tells us nothing. Guessing
    from it would be inventing an answer, so it returns None and §2.4 parks the
    question with the employer's wording intact.
    """
    return _CORE_KEYS.get(field_name)


class LeverAdapter:
    """Drives a Lever application form."""

    name = "lever"

    @staticmethod
    def matches(url: str) -> bool:
        return bool(_URL_RE.match(url))

    @staticmethod
    def company_slug(url: str) -> str | None:
        match = _URL_RE.match(url)
        return match.group("company") if match else None

    @staticmethod
    def external_id(url: str) -> str | None:
        match = _URL_RE.match(url)
        return match.group("job_id") if match else None

    async def _guard_automation_blocks(self, page: Any) -> None:
        """Stop on a captcha rather than trying to get around it.

        CLAUDE.md §2.5 — a hard scope boundary, not a gap to close. Lever
        mounts one on its apply forms, so this fires in practice.
        """
        if await page.locator(SELECTORS["captcha"]).count():
            raise ManualCompletionRequired("captcha present; finish this application by hand")

    async def parse_posting(self, page: Any) -> ParsedPosting:
        url = page.url

        closed = bool(await page.locator(SELECTORS["closed_marker"]).count())

        async def _text(selector: str) -> str | None:
            locator = page.locator(selector).first
            if not await locator.count():
                return None
            return _clean_label(await locator.inner_text()) or None

        return ParsedPosting(
            url=url,
            external_id=self.external_id(url),
            title=await _text(SELECTORS["posting_title"]),
            location=await _text(SELECTORS["posting_location"]),
            description_raw=await _text(SELECTORS["posting_body"]),
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
        seen_groups: set[str] = set()

        for index in range(count):
            control = controls.nth(index)
            key = await control.get_attribute("name") or await control.get_attribute("id")
            if not key:
                continue

            tag = (await control.evaluate("el => el.tagName")).lower()
            input_type = (await control.get_attribute("type") or "").lower()
            multiple = await control.get_attribute("multiple") is not None
            role = (await control.get_attribute("role") or "").lower()
            css = await control.get_attribute("class") or ""
            kind = _kind_for(tag, input_type, key, multiple, role, css)

            # Lever repeats one name across every option of a checkbox or radio
            # group. That is one question, not one per option.
            if kind in (QuestionKind.RADIO, QuestionKind.CHECKBOX):
                if key in seen_groups:
                    continue
                seen_groups.add(key)

            label = await self._label_for(form, control, key)

            options = []
            if kind in (QuestionKind.SINGLE_SELECT, QuestionKind.MULTI_SELECT) and tag == "select":
                from packages.ats.base import Option

                nodes = control.locator("option")
                for opt_index in range(await nodes.count()):
                    node = nodes.nth(opt_index)
                    value = await node.get_attribute("value") or ""
                    if value:
                        options.append(
                            Option(label=_clean_label(await node.inner_text()), value=value)
                        )

            questions.append(
                Question(
                    key=key,
                    label=label,
                    kind=kind,
                    required=await control.get_attribute("required") is not None,
                    options=options,
                )
            )

        return questions

    async def _label_for(self, form: Any, control: Any, key: str) -> str:
        """The label exactly as the site words it.

        Falls back to the field name only when the page offers nothing, and a
        `cards[<uuid>]` name is not a question — §2.4 wants the employer's text,
        so an unlabelled field is reported as such rather than dressed up.
        """
        container = control.locator("xpath=ancestor::*[self::li or self::div][1]")
        if await container.count():
            label = container.locator(SELECTORS["label"]).first
            if await label.count():
                text = _clean_label(await label.inner_text())
                if text:
                    return text

        if key.startswith("cards["):
            return "(this question's wording could not be read from the page)"
        return key

    async def fill(self, page: Any, answers: dict[str, Any]) -> FillReport:
        raise NotImplementedError(
            "Lever fill is not implemented. enumerate_fields and parse_posting are "
            "verified against a live board; filling is not, and shipping an "
            "unverified fill path would put unchecked values on a real application."
        )

    async def submit(self, page: Any) -> Receipt:
        raise NotImplementedError("Lever submit is not implemented.")
