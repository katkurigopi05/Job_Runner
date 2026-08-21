"""Write a cover letter, under the same rule that governs the résumé.

§2.1 says rewriting may not add a fact the source does not support. That was
written about bullets, and a cover letter is where it matters more, not less:
a bullet gets skimmed, a letter gets read closely and then asked about in an
interview. So the letter goes through `guard.check` exactly like a rewrite
does, against the same corpus.

## Why this is stricter than the bullet path in one place and looser in another

**Stricter:** there is no fallback. A rejected bullet falls back to the
original, which is always safe. A letter has no original — the alternative to
a bad letter is no letter, and that is the right alternative. `write()`
returns `None` and says why, rather than shipping something the guard refused.

**Looser:** no vocabulary-overlap floor. That check exists to catch a bullet
being replaced wholesale rather than rewritten, and a letter is not a rewrite
of anything, so there is nothing to compare it against. The entity check does
all the work here.

## What it will not discuss

Salary, work authorization, sponsorship, notice period. §2.2 keeps those
verbatim from the profile because a wrong answer has legal consequences, and a
letter that volunteers them is generating exactly the answer that rule exists
to prevent. The prompt says so and `_mentions_protected` checks it, because
the prompt is a request and this is a rule.
"""

from __future__ import annotations

import re

import structlog
from pydantic import BaseModel

from packages.llm.prompts import COVER_LETTER_SYSTEM
from packages.llm.provider import LLMProvider
from packages.llm.router import temperature_for
from packages.tailor.guard import GuardReport, SourceCorpus, check

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = COVER_LETTER_SYSTEM.text

#: Their guidance says 350-420 body words; ours is wider at the bottom because
#: a short honest letter beats a padded one, and the guard makes padding
#: expensive anyway.
MIN_WORDS = 120
MAX_WORDS = 450

#: Openers that say nothing. A letter starting here has not started.
_DEAD_OPENERS = re.compile(
    r"^\s*(?:dear\s+hiring\s+manager,?\s*)?"
    r"(i am (?:writing|excited|thrilled|reaching out)|i would like to apply)",
    re.I,
)

#: §2.2 territory. The letter does not go here even to say something true.
_PROTECTED = re.compile(
    r"\b(salary|compensation expectation|visa|sponsor(?:ship)?|work authoriz\w*|"
    r"notice period|green card|h-?1b)\b",
    re.I,
)


class CoverLetter(BaseModel):
    """A letter the guard accepted, or the reason there is not one."""

    text: str = ""
    accepted: bool = False
    rejected_reason: str | None = None
    entities_checked: int = 0
    word_count: int = 0

    @property
    def usable(self) -> bool:
        return self.accepted and bool(self.text.strip())


def _user_prompt(resume_text: str, job_description: str, company: str | None) -> str:
    heading = f"Company: {company}\n\n" if company else ""
    return (
        f"{heading}Job description:\n{job_description.strip()[:4000]}\n\n"
        f"Résumé:\n{resume_text.strip()[:6000]}\n\n"
        "Write the cover letter."
    )


def _clean(raw: str) -> str:
    text = raw.strip()
    # Models like to wrap prose in fences or announce themselves first.
    text = re.sub(r"^```[\w]*\n|\n```$", "", text).strip()
    text = re.sub(r"^(here is|here's)[^\n]*\n+", "", text, flags=re.I).strip()
    return text


def mentions_protected(text: str) -> str | None:
    """The §2.2 term the letter raised, if it raised one."""
    match = _PROTECTED.search(text)
    return match.group(0) if match else None


def vet(candidate: str, corpus: SourceCorpus) -> tuple[bool, str | None, GuardReport]:
    """Decide whether a letter may be used. No fallback if it may not."""
    report = check(candidate, corpus)

    if not candidate.strip():
        return False, "model returned nothing", report

    if not report.ok:
        return False, report.summary(), report

    protected = mentions_protected(candidate)
    if protected is not None:
        return (
            False,
            f"letter raises {protected!r}; §2.2 keeps that verbatim from the profile",
            report,
        )

    words = len(candidate.split())
    if words < MIN_WORDS:
        return False, f"{words} words is too short to say anything specific", report
    if words > MAX_WORDS:
        return False, f"{words} words is longer than anyone will read", report

    if _DEAD_OPENERS.match(candidate):
        return False, "opens with a filler sentence that says nothing", report

    return True, None, report


async def write(
    provider: LLMProvider,
    *,
    resume_text: str,
    job_description: str,
    corpus: SourceCorpus,
    company: str | None = None,
) -> CoverLetter:
    """Write one letter, or return the reason there is not one.

    Unlike `tailor_bullet`, this never falls back. A bullet has an original to
    return to; a letter does not, and shipping one the guard refused would put
    an unsupported claim in the owner's name on a real application.
    """
    try:
        raw = await provider.complete(
            SYSTEM_PROMPT,
            _user_prompt(resume_text, job_description, company),
            max_tokens=900,
            temperature=temperature_for("write_cover_letter"),
        )
    except Exception as exc:  # noqa: BLE001 - a provider failure means no letter
        log.warning("cover_letter_provider_failed", error=type(exc).__name__)
        return CoverLetter(rejected_reason=f"provider error: {type(exc).__name__}")

    candidate = _clean(raw)
    accepted, reason, report = vet(candidate, corpus)

    if not accepted:
        # Never log the letter itself — §10 keeps résumé-derived text out.
        log.info("cover_letter_rejected", reason=reason, violations=len(report.violations))
        return CoverLetter(
            rejected_reason=reason,
            entities_checked=report.checked,
            word_count=len(candidate.split()),
        )

    return CoverLetter(
        text=candidate,
        accepted=True,
        entities_checked=report.checked,
        word_count=len(candidate.split()),
    )
