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
from dataclasses import replace

import structlog
from pydantic import BaseModel

from packages.llm.prompts import COVER_LETTER_SYSTEM
from packages.llm.provider import LLMProvider
from packages.llm.router import temperature_for
from packages.tailor.guard import EntityKind, GuardReport, SourceCorpus, _index, check
from packages.tailor.keywords import analyze, borrowed_terms

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
    # `citizen` is §2.2 the same way `visa` is: a letter volunteering it is
    # answering a work-authorization question nobody asked, and the profile is
    # where that answer lives.
    r"\b(salary|compensation expectation|visa|sponsor(?:ship)?|work authoriz\w*|"
    r"citizen(?:ship)?|notice period|green card|h-?1b)\b",
    re.I,
)


class CoverLetter(BaseModel):
    """A letter the guard accepted, or the reason there is not one."""

    text: str = ""
    accepted: bool = False
    rejected_reason: str | None = None
    entities_checked: int = 0
    word_count: int = 0
    #: Sentences `sift` removed before the letter was judged. A letter that
    #: survives only by having most of itself deleted is worth noticing, and
    #: the count is the cheapest way to notice it.
    sentences_dropped: int = 0

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


#: The address lines. A greeting and a sign-off are scaffolding, not prose:
#: they assert nothing about the candidate, and "Dear Hiring Manager" reads to
#: the guard as an unsupported claim about a `Manager` — which is how a letter
#: that says nothing wrong came to be refused for its first four words.
#: Matched narrowly and set aside before anything is judged, so neither can
#: smuggle a claim past the check.
_GREETING = re.compile(r"\A\s*(dear\b[^\n]{0,60}?[,:])[ \t]*\n?", re.I)
_SIGNOFF = re.compile(
    r"\n[ \t]*((?:sincerely|regards|best regards|best|kind regards|thank you|"
    r"yours(?: truly| sincerely)?)\s*,?[ \t]*\n?[^\n]{0,60})\s*\Z",
    re.I,
)

#: Split at a terminator only when whitespace and a capital follow it.
#:
#: Splitting on the period alone cuts "React and Next.js" in half and leaves
#: "js experience." standing as a sentence of its own — which then passes the
#: guard, because "js" traces to nothing worth objecting to. A dotted token
#: has no space after the dot, so requiring one keeps it whole.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[\"\'(\[]?[A-Z])")


def _strip_addressing(text: str) -> tuple[str, str, str]:
    """Split a letter into greeting, body, sign-off. Either end may be empty."""
    greeting = ""
    signoff = ""

    match = _GREETING.match(text)
    if match:
        greeting = match.group(1).strip()
        text = text[match.end() :]

    match = _SIGNOFF.search(text)
    if match:
        signoff = match.group(1).strip()
        text = text[: match.start()]

    return greeting, text.strip(), signoff


def addressed(corpus: SourceCorpus, company: str | None) -> SourceCorpus:
    """The corpus plus the one outside fact a letter must be able to state.

    `SourceCorpus` warns that widening it is how a fabrication becomes
    permissible and should never be done quietly, so this is the whole of the
    widening: the company name, which the caller supplied and which the letter
    is addressed to. Naming the recipient asserts nothing about the candidate.

    The job description deliberately does *not* go in. It is what the model is
    being asked to write toward, and indexing it would make every requirement
    in the posting claimable — the exact failure §2.1 exists to prevent.
    """
    if not company or not company.strip():
        return corpus
    return replace(
        corpus,
        tokens=corpus.tokens | _index(company),
        text=f"{corpus.text}\n{company.lower()}",
    )


#: A violation the whole letter cannot survive.
#:
#: The guard reports *what kind* of thing it could not trace, and the kinds
#: fall either side of the line this module cares about. An unsupported proper
#: noun, acronym, number or year is a fabricated tool, employer, credential or
#: metric — a claim the owner would have to defend in an interview. An
#: unsupported `scope` word is seniority or role vocabulary: "Manager" in a
#: greeting, "Staff Platform Engineer" in the sentence naming the opening.
#:
#: So the two are handled differently on purpose. A fabricated credential ends
#: the letter, because §2.1 has no fallback to offer here. A role word ends
#: only its own sentence, because the letter naming the job it is for is not a
#: claim about the candidate and refusing over it is what made this module
#: refuse nearly everything.
_FATAL_KINDS = frozenset(
    {EntityKind.NUMBER, EntityKind.PROPER_NOUN, EntityKind.ACRONYM, EntityKind.YEAR}
)


class SiftReport(BaseModel):
    """What survived the sentence-level pass, and how much did not."""

    text: str = ""
    kept: int = 0
    dropped: int = 0
    entities_checked: int = 0
    #: Set when a sentence fabricated rather than merely over-named. The
    #: caller must refuse the letter; there is nothing here worth keeping.
    fatal_reason: str | None = None


def sift(body: str, corpus: SourceCorpus, *, forbidden: tuple[str, ...] = ()) -> SiftReport:
    """Drop the sentences that only over-name; refuse outright on a fabrication.

    `vet` judges the whole letter at once, and for a bullet that is the right
    shape — one unsupported entity means fall back to the original. A letter
    has no original, so all-or-nothing meant a single stray clause cost the
    entire draft, and "Dear Hiring Manager" is a stray clause: `Manager` traces
    to nothing in a résumé that never used the word.

    Checking per sentence keeps the property §2.1 actually asks for — every
    claim traceable — without spending the letter on its own salutation. What
    it must not do is soften a fabrication into a deletion, so `_FATAL_KINDS`
    draws that line and this returns `fatal_reason` instead of quietly
    dropping the sentence.

    Paragraph structure is preserved; a paragraph emptied of every sentence
    disappears rather than leaving a gap.
    """
    kept_paragraphs: list[str] = []
    kept = dropped = checked = 0

    for paragraph in re.split(r"\n\s*\n", body):
        survivors: list[str] = []

        for raw_sentence in _SENTENCE_BREAK.split(paragraph):
            sentence = raw_sentence.strip()
            if not sentence:
                continue

            if borrowed_terms(corpus.text, sentence, forbidden):
                dropped += 1
                continue

            report = check(sentence, corpus, scope=None)
            checked += report.checked

            if not report.ok:
                fatal = {v.entity.kind for v in report.violations} & _FATAL_KINDS
                if fatal:
                    # Never log the sentence — §10 keeps résumé-derived text out.
                    log.info("cover_letter_fabricated", kinds=sorted(k.value for k in fatal))
                    return SiftReport(
                        entities_checked=checked,
                        dropped=dropped + 1,
                        kept=kept,
                        fatal_reason=report.summary(),
                    )
                dropped += 1
                continue

            survivors.append(sentence)
            kept += 1

        if survivors:
            kept_paragraphs.append(" ".join(survivors))

    if dropped:
        log.info("cover_letter_sentences_dropped", dropped=dropped, kept=kept)

    return SiftReport(
        text="\n\n".join(kept_paragraphs),
        kept=kept,
        dropped=dropped,
        entities_checked=checked,
    )


def vet(candidate: str, corpus: SourceCorpus) -> tuple[bool, str | None, GuardReport]:
    """Decide whether a letter may be used. No fallback if it may not.

    The addressing is set aside before the guard sees the text, for the reason
    `_GREETING` gives: "Dear Hiring Manager," asserts nothing about the
    candidate, and `Manager` traces to no résumé that does not contain the
    word. `write` already strips it before sifting — and then joined it back
    on before calling this, so the guard read it anyway and refused clean
    letters on their first four words. Stripping here rather than there covers
    both callers, since this is the function that decides.

    Everything else still judges the whole letter: a sign-off that raises
    salary is §2.2 wherever it sits, and the length bound measures what a
    reader actually receives.
    """
    _, body, _ = _strip_addressing(candidate)
    report = check(body, corpus)

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

    Two passes, and the order matters. `sift` removes the sentences that do
    not trace to the source, then `vet` judges what is left as a whole —
    protected topics, length, a dead opener. A letter that survives only
    because sifting deleted most of it fails the length bound rather than
    going out as a stub, which is the honest outcome and a named one.
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

    # The company is the only thing outside the résumé this letter may name.
    letter_corpus = addressed(corpus, company)
    greeting, body, signoff = _strip_addressing(_clean(raw))

    # Terms the posting asks for that the résumé cannot back. A sentence that
    # reaches for one is keyword-stuffing beyond what §2.1 permits.
    sifted = sift(body, letter_corpus, forbidden=tuple(analyze(job_description, corpus).missing))

    if sifted.fatal_reason is not None:
        # A fabricated credential is not a sentence to delete. §2.1 offers no
        # fallback for a letter, so the alternative to a bad letter is none.
        return CoverLetter(
            rejected_reason=sifted.fatal_reason,
            entities_checked=sifted.entities_checked,
            sentences_dropped=sifted.dropped,
        )

    candidate = "\n\n".join(part for part in (greeting, sifted.text, signoff) if part)
    accepted, reason, report = vet(candidate, letter_corpus)

    if not accepted:
        # Never log the letter itself — §10 keeps résumé-derived text out.
        log.info("cover_letter_rejected", reason=reason, violations=len(report.violations))
        return CoverLetter(
            rejected_reason=reason,
            entities_checked=report.checked + sifted.entities_checked,
            word_count=len(candidate.split()),
            sentences_dropped=sifted.dropped,
        )

    return CoverLetter(
        text=candidate,
        accepted=True,
        entities_checked=report.checked + sifted.entities_checked,
        word_count=len(candidate.split()),
        sentences_dropped=sifted.dropped,
    )
