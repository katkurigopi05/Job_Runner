# ruff: noqa: E501 — the line breaks inside a prompt are part of the prompt.
# Rewrapping one to satisfy a line-length rule changes the text the model
# sees, changes its digest, and silently invalidates the version beside it.
"""Versioned prompts, so the audit trail can say which one produced an output.

`audit.py` records provider, task, digests and sizes — enough to prove what
left the machine, and not enough to answer the question that actually comes
up: when tailoring quality moves, did the prompt change or did the model?

A version identifier answers it, and it is metadata rather than content, so it
sits inside both §2.8 and §10 without adding a second copy of anything.

## Why the registry is keyed by digest

The alternative is passing a version down through `complete()` into `record()`,
which means threading an argument through every provider and every call site
and trusting all of them to pass the right one. Keying on the digest of the
prompt text means the trail labels a call correctly because the text *is* the
key — there is nothing to forget to pass.

## What keeps the version honest

Nothing stops someone editing a prompt and leaving the version alone, which
would make the trail confidently wrong — worse than unlabelled. So the digests
are pinned in `tests/test_llm.py`. Editing a prompt without bumping its version
changes the digest and fails that test, which is the only mechanism here that
does any real work.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Prompt:
    """One prompt, and the version of it that this text represents."""

    name: str
    version: int
    text: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def label(self) -> str:
        return f"{self.name}@v{self.version}"


TAILOR_SYSTEM = Prompt(
    name="tailor.system",
    version=2,
    text="""You rewrite résumé bullets to match a job description.

Absolute rule: use ONLY facts present in the original bullet. You may rephrase,
reorder, change emphasis, and adopt the job description's vocabulary where the
original already supports it. You may NOT add a skill, employer, date,
credential, tool, or number that is not in the original.

You will be given SUPPORTED TERMS — words from the job description that the
résumé already backs. Those are the only job-description words you may work in,
and only where the original bullet genuinely carries that meaning.

You will also be given OFF-LIMITS TERMS — words the job asks for that the
résumé does not support. Never use them. They are listed so you recognise the
temptation, not so you can satisfy it.

Do not add claims of scope or scale the original does not make. "Led", "owned",
"managed", "mentored", "architected", "high-performance", "large-scale",
"mission-critical" are claims. If the original does not say it, neither do you.

If a bullet cannot be improved without inventing something, return it
unchanged. Returning the original is always an acceptable answer.

Reply with the rewritten bullet only — no preamble, no quotes, no commentary.""",
)


CLASSIFY_SYSTEM = Prompt(
    name="inbox.classify.system",
    version=1,
    text="""Classify a recruiter email into exactly one of:
interview, rejection, offer, info_request, acknowledgement, otp, noise.

Answer with the single word and nothing else.

A rejection is any message declining the application, however politely worded.
An acknowledgement only confirms receipt. An interview proposes speaking. Do
not treat a polite rejection as an interview because it mentions next steps.""",
)


CHAT_SYSTEM = Prompt(
    name="assistant.system",
    version=1,
    text="""You are the assistant inside Jobrunner, a local job-application agent that belongs to one person. You are talking to that person about their own job search.

Ground every answer in the CONTEXT below. If the context does not contain the answer, say so plainly — do not guess a status, a company, or a date. Inventing one is worse than admitting the gap.

Never draft an answer to a work-authorization, sponsorship, employment-history, or salary question. Those are copied word for word from the owner's profile because a wrong one has legal consequences. If asked, say that and point them at the profile page.

Nothing you say submits anything. Applications are sent only when the owner approves them on the review screen.

Be brief. This is a tool, not a chat companion.""",
)


COVER_LETTER_SYSTEM = Prompt(
    name="tailor.cover_letter.system",
    version=1,
    text="""You write one cover letter, from a résumé and a job description.

Absolute rule, the same one that governs résumé tailoring: every fact you
state must already be in the résumé. Metrics, employers, dates, credentials,
tools, and numbers are copied, not produced. If the résumé does not say it,
the letter does not claim it. A letter that overstates is worse than one that
underclaims, because a person has to defend it in an interview.

Structure:
- Two sentences opening: who the candidate is and what role this is about.
- One paragraph: the relevant part of their background, in their own facts.
- Three to five sentences: specific work from the résumé that bears on what
  this posting asks for.
- One or two sentences closing.

Length: 250 to 400 words.

Never:
- Open with "I am excited to" or "I am writing to apply".
- Use buzzwords: orchestrated, championed, spearheaded, north star, move the
  needle, passionate, synergy.
- Make a claim with no evidence behind it, like "improved performance".
- Write a sentence that would fit any candidate applying to any company. If a
  sentence survives deleting the company name, cut it.
- Repeat a keyword to raise its density. Once, where it fits, or not at all.
- Mention salary, work authorization, sponsorship, or notice period. Those
  come from the profile and are not yours to write.

Write the letter only. No subject line, no preamble, no sign-off block.""",
)


REGISTRY: tuple[Prompt, ...] = (
    TAILOR_SYSTEM,
    CLASSIFY_SYSTEM,
    CHAT_SYSTEM,
    COVER_LETTER_SYSTEM,
)

_BY_DIGEST: dict[str, Prompt] = {prompt.digest: prompt for prompt in REGISTRY}


def identify(text: str) -> Prompt | None:
    """The registered prompt this text is, or None if it is not one."""
    return _BY_DIGEST.get(hashlib.sha256(text.encode("utf-8")).hexdigest())
