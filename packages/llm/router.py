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

**Temperature is routed here too**, and for the same reason the provider is:
§7's tasks want opposite things from a model, so a single global setting is
wrong for most of them. See `TEMPERATURES` below.
"""

from __future__ import annotations

import os
from typing import TypeVar

import structlog
from pydantic import BaseModel

from packages.llm.provider import (
    DEFAULT_TEMPERATURE,
    LLMError,
    LLMProvider,
    build_provider,
)

log = structlog.get_logger(__name__)

#: Matches `LLMProvider.complete_json`'s schema parameter.
T = TypeVar("T", bound=BaseModel)

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
#: `best_available()` walks this in order. **OpenRouter is deliberately not in
#: it.** It is a real provider and `build_provider` knows it, but a key sitting
#: in `.env` must not silently redirect every "auto" task to it: OpenRouter
#: forwards to an upstream the audit trail cannot name, and on a cloaked
#: `stealth/*` route the vendor is undisclosed by design. §2.8 asks that the one
#: permitted upload be auditable, so that route is something the owner opts into
#: by naming it — `LLM_PROVIDER=openrouter`, or `LLM_TASK_TAILOR=openrouter` —
#: never something inherited from having configured a key.
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
    # Same source the providers themselves read, including `.env` — a key
    # the owner put in the documented place must not read as unconfigured.
    from packages.core.config import get_settings

    settings = get_settings()
    if name == "gemini":
        return bool(os.environ.get("GEMINI_API_KEY") or settings.gemini_api_key)
    if name == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY") or settings.anthropic_api_key)
    if name == "openrouter":
        return bool(os.environ.get("OPENROUTER_API_KEY") or settings.openrouter_api_key)
    if name == "ollama_cloud":
        # Named model, not a key. Ollama needs no credential once the local
        # daemon is signed in, so there is nothing else that could mean "the
        # owner set this up" — and requiring the model to be named keeps the
        # provider unreachable until someone types it.
        return bool(os.environ.get("OLLAMA_CLOUD_MODEL") or settings.ollama_cloud_model)
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


#: Temperature per §7 task. The numbers are less interesting than the reasons.
#:
#: - **classify_inbound_email** picks one word from a fixed set. Variance here
#:   is pure downside; there is no creative version of "rejection".
#: - **map_form_field** matches a question to a profile key. Same shape.
#: - **tailor_resume** is bounded by the fabrication guard, which discards
#:   anything inventive. A creative model does not produce better résumés
#:   here, it produces a higher rejection rate and a silent fallback to the
#:   original bullet — the tailorer appearing to do nothing.
#: - **write_cover_letter** is the one task where variance buys something, and
#:   the guard still checks the result.
#: - **answer_open_ended_question** goes on a real application under the
#:   owner's name, so it stays close to the evidence.
TEMPERATURES: dict[str, float] = {
    "classify_inbound_email": 0.0,
    "map_form_field": 0.0,
    "tailor_resume": 0.3,
    "write_cover_letter": 0.7,
    "answer_open_ended_question": 0.3,
}


def temperature_for(task: str) -> float:
    """The temperature this task should run at.

    An unknown task gets the conservative default rather than a vendor's,
    which is around 1.0 for most of them and wrong for nearly everything here.
    """
    return TEMPERATURES.get(task, DEFAULT_TEMPERATURE)


#: The §7 tasks the owner may point at a provider, and the setting for each.
#:
#: Deliberately not every task. `classify_inbound_email` and the assistant read
#: recruiter mail and chat context; §2.8 permits one third-party upload and
#: that is not it, so those stay local and are not listed here. A setting that
#: could move them would be a way to opt out of a non-negotiable by editing
#: `.env`.
CHOOSABLE_TASKS: dict[str, str] = {
    "tailor_resume": "llm_task_tailor",
    "write_cover_letter": "llm_task_cover_letter",
    "answer_open_ended_question": "llm_task_open_ended",
}

#: The provider a fallback lands on. Never the stub: §7 is explicit that canned
#: text must not reach a real application, and the whole value of StubProvider's
#: marker is that "nothing is configured" stays visible.
LOCAL_PROVIDER = "ollama"


def cloud_for_tailoring() -> str | None:
    """The remote provider a tailoring comparison should run against.

    None when there is no remote side to compare with — no key anywhere, or the
    owner pinned tailoring to the local model and configured nothing else. The
    caller reports that as a candidate carrying the reason rather than quietly
    showing one column, because a comparison missing half of itself reads as a
    verdict.

    An explicit `LLM_TASK_TAILOR` wins when it names a remote provider: it is
    the provider real tailoring would use, so it is the one worth comparing
    against. `openrouter` is reachable this way and only this way — it is kept
    out of `QUALITY_ORDER` on purpose, and a comparison must not be the back
    door that reintroduces it as a default.
    """
    chosen = _chosen("tailor_resume")
    if chosen and chosen not in (LOCAL_PROVIDER, "stub"):
        return chosen if _configured(chosen) else None

    for name in QUALITY_ORDER:
        if name in (LOCAL_PROVIDER, "stub"):
            continue
        if _configured(name):
            return name
    return None


#: Remote providers a comparison may be pointed at, in the order shown.
#:
#: Wider than `QUALITY_ORDER` by exactly two: `openrouter` and `ollama_cloud`.
#: Both omissions are deliberate and stay — §7 keeps them out so a line in
#: `.env` cannot make either a default for anything. This list is not a
#: default; nothing here is used unless the owner names it for one comparison.
#:
#: `ollama_cloud` is the one whose omission is easiest to undo by accident,
#: because it looks local. It shares the `ollama` base URL, it needs no API
#: key, and `localhost:11434` appears in the request either way. Putting it in
#: `QUALITY_ORDER` would send every "auto" task to Ollama's servers the moment
#: `OLLAMA_CLOUD_MODEL` was set, with nothing in the configuration that reads
#: as having asked for that.
COMPARABLE_CLOUD = ("anthropic", "gemini", "openrouter", "ollama_cloud")


def comparable_clouds() -> list[str]:
    """Remote providers that could answer a comparison right now.

    Configured only — a provider with no key is not offered, so pasting a key
    remains the thing that makes a route reachable at all. §7 asks that
    acquiring a route whose recipient cannot be named take typing the word
    rather than pasting a key; naming it per comparison is that typing, and the
    UI says what the route implies before the owner picks it.
    """
    return [name for name in COMPARABLE_CLOUD if _configured(name)]


def is_comparable_cloud(name: str) -> bool:
    """Whether `name` may be the remote half of a comparison.

    The local model is excluded deliberately: it is already the other column,
    and a comparison of a model against itself is not a comparison. `stub` is
    excluded because canned output has nothing to say about a real posting.
    """
    return name in COMPARABLE_CLOUD and _configured(name)


def _chosen(task: str) -> str | None:
    """The provider the owner pinned this task to, if any."""
    from packages.core.config import get_settings

    field = CHOOSABLE_TASKS.get(task)
    if field is None:
        return None
    value = (getattr(get_settings(), field, "auto") or "auto").strip().lower()
    return None if value in ("", "auto") else value


class FallbackProvider:
    """A remote provider with the local model behind it.

    Wraps rather than replaces, so the primary is still tried first and still
    fails loudly for the reasons the module docstring gives. What it removes is
    the case §7's own `QuotaExceeded` message describes: "raise
    LLM_DAILY_REMOTE_CALLS, wait for the reset, or run a local provider". The
    third option was a manual instruction to a human; this is that option taken
    automatically, and recorded.

    `answered_by` is what the review screen reads. A résumé tailored by
    llama3.1 after the Gemini allowance ran out is a different document from
    one tailored by Gemini, and the owner approving it should be able to see
    which they are looking at.
    """

    def __init__(self, primary: LLMProvider, task: str) -> None:
        self.primary = primary
        self.task = task
        self.name = getattr(primary, "name", "unknown")
        self.answered_by = self.name

    def _local(self) -> LLMProvider:
        return build_provider(LOCAL_PROVIDER)

    def _should_retry(self, exc: Exception) -> bool:
        from packages.llm.quota import QuotaExceeded

        return isinstance(exc, (QuotaExceeded, LLMError))

    def _note(self, exc: Exception, local: LLMProvider) -> None:
        log.warning(
            "llm_fell_back_to_local",
            task=self.task,
            primary=self.name,
            reason=type(exc).__name__,
        )
        self.answered_by = f"{LOCAL_PROVIDER}:{getattr(local, 'model', '')}".rstrip(":")

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        self.answered_by = self.name
        try:
            return await self.primary.complete(
                system, user, max_tokens=max_tokens, temperature=temperature
            )
        except Exception as exc:
            if not self._should_retry(exc):
                raise
            local = self._local()
            self._note(exc, local)
            return await local.complete(
                system, user, max_tokens=max_tokens, temperature=temperature
            )

    async def complete_json(self, system: str, user: str, schema: type[T]) -> T:
        self.answered_by = self.name
        try:
            return await self.primary.complete_json(system, user, schema)
        except Exception as exc:
            if not self._should_retry(exc):
                raise
            local = self._local()
            self._note(exc, local)
            return await local.complete_json(system, user, schema)


def _for_task(task: str, preferred: str | None = None) -> LLMProvider:
    """Build a provider, recording which task asked for it.

    `preferred` is a request, not a fallback chain. If it is configured and
    broken, that surfaces — see the module docstring.

    Order: a provider the caller pinned in code wins over one the owner pinned
    in settings, because the in-code ones are the §2.8 locks rather than
    preferences. Then the owner's choice, then `best_available()`.
    """
    selected = preferred or _chosen(task) or best_available()
    provider = build_provider(selected)

    from packages.core.config import get_settings

    if (
        get_settings().llm_fallback_local
        and selected not in (LOCAL_PROVIDER, "stub")
        and _configured(LOCAL_PROVIDER)
    ):
        return FallbackProvider(provider, task)
    return provider


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
