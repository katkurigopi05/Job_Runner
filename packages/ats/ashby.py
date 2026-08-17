"""Ashby adapter — CLAUDE.md §8, third in the build order.

Built against the live DOM of a real Ashby posting. Each of the three ATSes has
turned out to be structurally different in a way no fixture written from memory
would have predicted:

- Greenhouse: a real `<form>`, but every dropdown is react-select — an
  `input type="text"` with `role="combobox"` and no `<option>` anywhere.
- Lever: a real `<form>` and native `<select>`.
- **Ashby: no `<form>` element at all.** Thirty-plus inputs rendered by React
  and never wrapped in one. An adapter that scopes to `form` finds nothing here
  and reports "no application form found" on a page that plainly has one.

Ashby's naming is its own thing too. System fields are prefixed
`_systemfield_` — `_systemfield_name`, `_systemfield_email`,
`_systemfield_resume`. Everything the employer added is named by bare uuid, and
some option groups are named by their own label text rather than a shared key.
So a uuid says nothing about what is being asked and the label is the whole
question — the §2.4 case again, arrived at from a third direction.
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

#: https://jobs.ashbyhq.com/<company>/<posting-uuid>[/application]
_URL_RE = re.compile(
    r"^https?://jobs\.ashbyhq\.com/(?P<company>[\w.-]+)/(?P<job_id>[0-9a-f-]{16,})",
    re.I,
)

SELECTORS: dict[str, str] = {
    # There is no <form>. The application lives in the page, so the scope is
    # the document and the field selector has to be the thing that narrows.
    "form": "body",
    "posting_title": "h1",
    "posting_location": "[class*='location'], [data-highlight]",
    "posting_body": "[class*='_description'], [class*='jobPosting'], main",
    "fields": (
        "input:not([type='hidden']):not([type='submit']):not([type='button']), textarea, select"
    ),
    "field_container": "[class*='_fieldEntry'], [class*='field'], div",
    "label": "label, [class*='_label']",
    "submit_button": "button[type='submit'], button:has-text('Submit Application')",
    "confirmation": "[class*='confirmation'], [class*='thank'], [class*='_success']",
    "captcha": (
        "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], "
        ".g-recaptcha, .h-captcha, iframe[title*='challenge']"
    ),
    "closed_marker": (
        "text=/no longer accepting applications/i, "
        "text=/this job is closed/i, text=/position has been filled/i"
    ),
}

#: Ashby prefixes the fields every posting has. Everything else is a uuid.
_SYSTEM_PREFIX = "_systemfield_"

_CORE_KEYS = {
    "_systemfield_name": "full_name",
    "_systemfield_email": "email",
    "_systemfield_phone": "phone",
    "_systemfield_resume": "resume",
    "_systemfield_location": "location",
    "_systemfield_linkedin": "linkedin",
    "_systemfield_github": "github",
    "_systemfield_website": "website",
}


def profile_key_for(field_name: str) -> str | None:
    """The profile key an Ashby field maps to, or None when only a human knows.

    A bare uuid describes nothing. Guessing from it would be inventing an
    answer, so §2.4 parks the question with the employer's wording instead.
    """
    return _CORE_KEYS.get(field_name)


def is_employer_question(field_name: str) -> bool:
    """True for anything the employer added rather than Ashby's own fields."""
    return not field_name.startswith(_SYSTEM_PREFIX)


class AshbyAdapter:
    """Drives an Ashby application form."""

    name = "ashby"

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
        """Stop on a captcha rather than trying to get around it. §2.5."""
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
        """Walk the real page. There is no form to scope to, so the page is it."""
        await self._guard_automation_blocks(page)

        scope = page.locator(SELECTORS["form"]).first
        controls = scope.locator(SELECTORS["fields"])
        count = await controls.count()
        if count == 0:
            raise SiteError("no application fields found on page")

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

            # Option groups repeat a name. Ashby also names some groups by
            # their own option text, so the collapse is by key either way.
            if kind in (QuestionKind.RADIO, QuestionKind.CHECKBOX):
                if key in seen_groups:
                    continue
                seen_groups.add(key)

            label = await self._label_for(control, key)

            options: list[Option] = []
            if kind in (QuestionKind.SINGLE_SELECT, QuestionKind.MULTI_SELECT) and tag == "select":
                nodes = control.locator("option")
                for opt_index in range(await nodes.count()):
                    node = nodes.nth(opt_index)
                    value = await node.get_attribute("value") or ""
                    if value:
                        options.append(
                            Option(label=_clean_label(await node.inner_text()), value=value)
                        )

            # Ashby marks required with aria-required as well as the attribute.
            required = (
                await control.get_attribute("required") is not None
                or (await control.get_attribute("aria-required")) == "true"
            )

            questions.append(
                Question(key=key, label=label, kind=kind, required=required, options=options)
            )

        return questions

    async def _label_for(self, control: Any, key: str) -> str:
        """The label exactly as the site words it.

        A bare uuid is not a question. When the page offers no label, that is
        reported rather than dressed up — §2.4 wants the employer's text, and a
        uuid presented as a question is worse than admitting it is missing.
        """
        container = control.locator("xpath=ancestor::div[1]")
        if await container.count():
            label = container.locator(SELECTORS["label"]).first
            if await label.count():
                text = _clean_label(await label.inner_text())
                if text:
                    return text

        if is_employer_question(key):
            return "(this question's wording could not be read from the page)"
        return key.removeprefix(_SYSTEM_PREFIX).replace("_", " ")

    async def fill(self, page: Any, answers: dict[str, Any]) -> FillReport:
        raise NotImplementedError(
            "Ashby fill is not implemented. parse_posting and enumerate_fields are "
            "verified against a live board; filling is not, and an unverified fill "
            "path would put unchecked values on a real application."
        )

    async def submit(self, page: Any) -> Receipt:
        raise NotImplementedError("Ashby submit is not implemented.")
