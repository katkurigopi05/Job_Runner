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
from packages.ats.form import field_selector, fill_form, submit_form
from packages.ats.greenhouse import _clean_label, _kind_for
from packages.ats.navigate import (
    FORM_READY_TIMEOUT_MS,
    POSTING_READY_TIMEOUT_MS,
    application_route,
    longest_text,
)
from packages.ats.navigate import wait_for_form as _wait_for_form
from packages.ats.navigate import wait_for_posting as _wait_for_posting

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
    # Ashby's left pane is a list of `<h2>heading</h2><p>value</p>` sections
    # inside one `[data-highlight]` container. Reading the container whole
    # gives "Location United Kingdom; Germany; New York; Poland Employment
    # Type Full time Location Type Remote Department Operations", which
    # `locality.py` then has to make a region decision from — and hard filters
    # exclude, so getting it wrong drops the posting and says nothing.
    "posting_section": "div:has(> h2)",
    "posting_section_heading": "h2",
    "posting_section_value": "p",
    # Fallback for a posting with no details pane at all.
    "posting_location": "[class*='location']",
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

#: Ashby puts the form on `/application`.
APPLICATION_SEGMENT: str | None = "application"

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

    @staticmethod
    def profile_key_for(field_name: str) -> str | None:
        """The profile key this field takes its answer from, or None.

        The module-level function is the implementation; this is how
        `build_answers` reaches it without importing one adapter by name.
        """
        return profile_key_for(field_name)

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
            location=await self._location(page),
            description_raw=await longest_text(page, SELECTORS["posting_body"]),
            closed=closed,
        )

    async def _location(self, page: Any) -> str | None:
        """Where the job is, from the details pane's own Location section.

        `Location Type` is folded in when it is there: `locality.reads_as_remote`
        reads the location field, and a posting naming four countries with a
        mode of Remote is a different job from one naming four offices. Ashby
        states the two separately and nothing else would carry the distinction.
        """
        sections = await self._sections(page)
        where = sections.get("location")
        mode = sections.get("location type")

        if not where:
            locator = page.locator(SELECTORS["posting_location"]).first
            if not await locator.count():
                return None
            return _clean_label(await locator.inner_text()) or None

        if mode and mode.lower() not in where.lower():
            return f"{where} ({mode})"
        return where

    async def _sections(self, page: Any) -> dict[str, str]:
        """The details pane as `{heading: value}`, lowercased headings."""
        found: dict[str, str] = {}
        blocks = page.locator(SELECTORS["posting_section"])
        for index in range(await blocks.count()):
            block = blocks.nth(index)
            heading = block.locator(SELECTORS["posting_section_heading"]).first
            value = block.locator(SELECTORS["posting_section_value"]).first
            if not await heading.count() or not await value.count():
                continue
            name = _clean_label(await heading.inner_text()).lower()
            text = _clean_label(await value.inner_text())
            if name and text:
                found.setdefault(name, text)
        return found

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
                Question(
                    key=key,
                    label=label,
                    kind=kind,
                    required=required,
                    options=options,
                    selector=field_selector(
                        await control.get_attribute("id"), await control.get_attribute("name")
                    ),
                )
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
        """Fill what we have answers for. Never invent one."""
        return await fill_form(self, page, answers, selectors=SELECTORS)

    async def submit(self, page: Any) -> Receipt:
        """Click submit and capture what the site says back."""
        return await submit_form(self, page, selectors=SELECTORS)
