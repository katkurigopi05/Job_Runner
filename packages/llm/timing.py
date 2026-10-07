"""What the daemon measured about a call: tokens read, tokens written, and how long.

Ollama answers every chat call with these and the provider used to keep only
the text. Two things follow from keeping the rest:

- **Reading and writing are told apart.** How long before the first word is
  loading plus reading the prompt; how fast the answer comes is a separate
  number. CLAUDE.md §14 needed a benchmark to see that one engine was faster
  at the second and slower at the first.
- **A filled context is visible.** Ollama cuts a prompt that is longer than
  the context it was given and returns no error. Tokens read plus tokens
  written reaching the cap is the sign.

This is a second file beside the audit trail, not more lines in it. The trail
answers "what left this machine", and `quota.py` counts its lines: one per
call. Each line here carries the digest the trail recorded for the same call,
so the two can be joined.

Counts, durations and that digest. Nothing of the prompt or the answer (§10).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from packages.llm import audit

log = structlog.get_logger(__name__)

#: The daemon reports durations in nanoseconds.
_NS_PER_MS = 1_000_000
_MS_PER_S = 1_000

#: Reply field -> what it is called here.
_COUNTS = {"prompt_eval_count": "prompt_tokens", "eval_count": "answer_tokens"}
_DURATIONS = {
    "load_duration": "load_ms",
    "prompt_eval_duration": "prompt_ms",
    "eval_duration": "answer_ms",
    "total_duration": "total_ms",
}


@dataclass(frozen=True)
class CallTiming:
    """One call, as the daemon measured it. A field it did not send is None."""

    at: str
    provider: str
    model: str | None
    #: The digest the audit trail holds for this call's user prompt.
    user_sha256: str
    prompt_tokens: int | None
    answer_tokens: int | None
    load_ms: float | None
    prompt_ms: float | None
    answer_ms: float | None
    total_ms: float | None
    #: The context the request capped the model at. None when it sent no cap,
    #: as for a model Ollama hosts.
    context_limit: int | None

    @property
    def before_first_token_ms(self) -> float | None:
        """Loading the model, if it had to, plus reading the prompt."""
        if self.prompt_ms is None:
            return None
        return (self.load_ms or 0.0) + self.prompt_ms

    @property
    def prompt_tokens_per_second(self) -> float | None:
        return _rate(self.prompt_tokens, self.prompt_ms)

    @property
    def answer_tokens_per_second(self) -> float | None:
        return _rate(self.answer_tokens, self.answer_ms)

    @property
    def context_full(self) -> bool:
        """Whether what was read and written reached the cap.

        Either the prompt was cut to fit, or the answer ran into the end of
        the context and the start of the prompt was dropped to make room. The
        daemon reports neither, and both mean the model did not have all of
        what it was handed.
        """
        if self.context_limit is None or self.prompt_tokens is None:
            return False
        return self.prompt_tokens + (self.answer_tokens or 0) >= self.context_limit


def _rate(tokens: int | None, ms: float | None) -> float | None:
    if tokens is None or not ms:
        return None
    return tokens / (ms / _MS_PER_S)


def _number(value: Any) -> int | None:
    """An integer the daemon sent, or None. A boolean is an int in Python."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def from_ollama(
    payload: dict[str, Any],
    *,
    provider: str,
    model: str | None,
    user: str,
    context_limit: int | None,
) -> CallTiming | None:
    """Read the measurements out of a chat reply. None when it carries none."""
    counts = {name: _number(payload.get(field)) for field, name in _COUNTS.items()}
    durations = {
        name: None if (ns := _number(payload.get(field))) is None else ns / _NS_PER_MS
        for field, name in _DURATIONS.items()
    }
    if all(value is None for value in (*counts.values(), *durations.values())):
        return None
    return CallTiming(
        at=datetime.now(UTC).isoformat(),
        provider=provider,
        model=model,
        user_sha256=audit.digest_of(user),
        prompt_tokens=counts["prompt_tokens"],
        answer_tokens=counts["answer_tokens"],
        load_ms=durations["load_ms"],
        prompt_ms=durations["prompt_ms"],
        answer_ms=durations["answer_ms"],
        total_ms=durations["total_ms"],
        context_limit=context_limit,
    )


def note(
    payload: dict[str, Any],
    *,
    provider: str,
    model: str | None,
    user: str,
    context_limit: int | None,
) -> CallTiming | None:
    """Keep a reply's measurements and say so if the context was full.

    Never raises. The answer has already arrived, and a line that cannot be
    written must not turn it into a failed call.
    """
    try:
        kept = from_ollama(
            payload, provider=provider, model=model, user=user, context_limit=context_limit
        )
        if kept is None:
            return None
        _append(kept)
    except Exception as exc:  # noqa: BLE001 - the measurement is optional; the answer is not
        # The type only: an OSError's message carries a path.
        log.error("llm_timing_write_failed", error=type(exc).__name__)
        return None

    log.info(
        "llm_call_timing",
        provider=kept.provider,
        model=kept.model,
        prompt_tokens=kept.prompt_tokens,
        answer_tokens=kept.answer_tokens,
        before_first_token_ms=kept.before_first_token_ms,
        answer_ms=kept.answer_ms,
    )
    if kept.context_full:
        log.warning(
            "llm_context_full",
            provider=kept.provider,
            model=kept.model,
            prompt_tokens=kept.prompt_tokens,
            answer_tokens=kept.answer_tokens,
            context_limit=kept.context_limit,
        )
    return kept


def _append(kept: CallTiming) -> None:
    path = timings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(kept)) + "\n")


def timings_path() -> Path:
    """Beside the audit trail, and outside storage/ for the same reason it is."""
    return audit.audit_path().with_name("llm-timings.jsonl")


def read_timings(limit: int | None = None) -> list[CallTiming]:
    """The measurements, oldest first. Empty when no local call has been made."""
    path = timings_path()
    if not path.is_file():
        return []
    kept = [CallTiming(**json.loads(line)) for line in path.read_text().splitlines() if line]
    return kept[-limit:] if limit else kept
