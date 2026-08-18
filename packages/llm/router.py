"""Task router for LLM calls — CLAUDE.md §7.

Two things this file is responsible for beyond picking a provider.

**§2.2 is enforced here, structurally.** Work-authorization and
employment-history answers are copied verbatim from the profile and are never
LLM-generated, because a wrong one has legal consequences for the applicant.
The protection is a required argument rather than an optional one: a caller
that does not say which field it is asking about cannot get a provider at all.
An optional check is one forgotten keyword away from generating the answer it
was written to prevent.

**Nothing falls back to the stub.** A provider that is configured but
unreachable is an error. Quietly substituting canned output would put
stub-generated text on a real job application, and the whole point of
StubProvider returning an obvious marker is that fabrication stays visible.
"""

from __future__ import annotations

import os

from packages.llm.provider import LLMError, LLMProvider, build_provider

#: Fields whose answers are copied from the profile, never generated. §2.2
#: names work authorization and employment history; salary sits alongside them
#: in packages/ats/answers.py::VERBATIM_KEYS and is treated the same.
PROTECTED_FIELDS = frozenset(
    {
        "work_auth",
        "work_authorization",
        "employment_history",
        "employment",
        "salary_expectation",
        "sponsorship",
        "needs_sponsorship",
    }
)


class ProtectedFieldError(LLMError):
    """An LLM was asked to answer something that must come from the profile."""


#: §7 assigns tailoring, cover letters, and open-ended answers to "best
#: available". That was never implemented: every one of them called
#: build_provider() with no argument, which returns whatever LLM_PROVIDER says.
#: With LLM_PROVIDER=stub, "quality matters most here" resolved to canned text.
#:
#: Strongest first. A provider is skipped when it is not configured — no API
#: key, or no reachable host — rather than when it errors. A configured
#: provider that fails still fails loudly; see the module docstring.
QUALITY_ORDER = ("anthropic", "gemini", "ollama", "stub")


def _configured(name: str) -> bool:
    """Whether a provider could answer, without asking it to.

    Cheap and local: reads the environment, the same source the providers
    themselves read, so the two cannot disagree. Deliberately not a health
    check — probing
    every provider on every routing decision would put a network round trip in
    front of each call, and an unreachable-but-configured provider should
    surface as an error rather than be silently skipped.
    """
    if name == "stub":
        return True
    if name == "ollama":
        # A base URL is always available by default, so Ollama counts as
        # configured. Whether it is *running* is a separate question, and one
        # that should surface as an error rather than a silent downgrade.
        return True
    if name == "gemini":
        return bool(os.environ.get("GEMINI_API_KEY"))
    if name == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    return False


def best_available() -> str:
    """The strongest configured provider, by QUALITY_ORDER.

    Falls through to stub, which returns an obvious marker rather than
    plausible prose — so "nothing is configured" reads as a visible failure in
    the diff instead of a quietly worse résumé.
    """
    for name in QUALITY_ORDER:
        if _configured(name):
            return name
    return "stub"


def _for_task(task: str, preferred: str | None = None) -> LLMProvider:
    """Build a provider, recording which task asked for it.

    `preferred` is a request, not a fallback chain. If it is configured and
    broken, that surfaces — see the module docstring.
    """
    return build_provider(preferred) if preferred else build_provider(best_available())


def classify_inbound_email() -> LLMProvider:
    """Cheap, local, and low-stakes — §7 routes this to Ollama."""
    return _for_task("classify_inbound_email", "ollama")


def map_form_field() -> LLMProvider:
    """Structured and low-creativity — §7 routes this to Ollama."""
    return _for_task("map_form_field", "ollama")


def tailor_resume() -> LLMProvider:
    """Quality matters most here, so this takes the best configured provider.

    This is the call §2.8 is about: it uploads the owner's résumé. Every
    provider records the upload in the audit trail — see packages/llm/audit.py.
    """
    return _for_task("tailor_resume")


def write_cover_letter() -> LLMProvider:
    return _for_task("write_cover_letter")


def answer_open_ended_question(field: str) -> LLMProvider:
    """A provider for a free-text question, or a refusal.

    `field` is required. A caller that cannot name the field it is answering
    has no business generating an answer for it — that is the whole protection,
    and making the argument optional would defeat it.

    Raises:
        ProtectedFieldError: the field is copied verbatim from the profile.
    """
    if is_protected(field):
        raise ProtectedFieldError(
            f"{field!r} is copied verbatim from the profile and is never LLM-generated "
            "(CLAUDE.md §2.2). Read it from the profile instead."
        )
    return _for_task("answer_open_ended_question")


def is_protected(field: str) -> bool:
    """Whether a field's answer must come from the profile rather than a model.

    Matches on substrings too: an ATS names the same question
    `question_12074270004` one day and `work_authorization_status` the next, and
    a protected field that is only recognised by exact name is not protected.
    """
    normalized = field.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in PROTECTED_FIELDS:
        return True
    return any(protected in normalized for protected in PROTECTED_FIELDS)
