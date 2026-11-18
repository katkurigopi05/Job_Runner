"""Structured output from any provider: find the JSON, validate it, ask again with the error.

`LLMProvider.complete_json` leans on each vendor's JSON mode and fails on the
first answer that does not parse. This asks through `complete`, which every
provider has (Ollama, Kaggle, Gemini, OpenRouter, the stub), and so works
where no JSON mode exists:

1. The answer's JSON is found wherever the model put it: the whole answer, a
   fenced block, or an object or array inside prose (`extract_json`).
2. It is validated against a Pydantic v2 model.
3. On invalid JSON or a `ValidationError`, the model is asked again with its
   previous answer and the exact errors, at most `max_retries` more times.
   Then `StructuredOutputError`, an `LLMError`, so a caller's existing
   provider-failure fallback catches it.

Every retry is one more call against the provider's allowance, and the free
tiers count calls, so the default is two. A provider's own failure is not
retried here: the providers pace and retry their transport themselves.

Nothing here logs an answer. It may carry résumé content (CLAUDE.md §10).
"""

from __future__ import annotations

import json
import re
from typing import Any, NoReturn

import structlog
from pydantic import BaseModel, ValidationError

from packages.llm.provider import LLMError, LLMProvider

log = structlog.get_logger(__name__)

#: A fenced block, with or without a language tag.
_FENCE_RE = re.compile(r"```[A-Za-z]*[ \t]*\n?(.*?)```", re.DOTALL)

#: Opening brackets tried per candidate before giving up. Bounds the work on a
#: long answer full of brackets: each try reads to the end of the text.
MAX_JSON_STARTS = 8

#: Validation errors quoted back to the model. The first few say what to fix.
MAX_FEEDBACK_ERRORS = 8

#: How much of a failed answer is quoted back, so a retry prompt stays bounded.
MAX_ECHOED_CHARS = 2000

DEFAULT_MAX_TOKENS = 1024


class StructuredOutputError(LLMError):
    """No attempt produced JSON that validated against the schema."""

    def __init__(self, schema: str, errors: tuple[str, ...]) -> None:
        super().__init__(
            f"No valid {schema} after {len(errors)} attempt(s). Last error: {errors[-1]}"
        )
        self.schema = schema
        #: What was wrong with each attempt, in order. Never the answer itself.
        self.errors = errors

    @property
    def attempts(self) -> int:
        return len(self.errors)


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} is not valid JSON")


_DECODER = json.JSONDecoder(parse_constant=_reject_constant)


def _candidates(text: str) -> list[str]:
    """Fenced blocks first, since a model that fences its JSON means that part."""
    fenced = [match.group(1).strip() for match in _FENCE_RE.finditer(text)]
    return [block for block in (*fenced, text.strip()) if block]


def _decode(candidate: str) -> tuple[Any, ValueError | None]:
    """The candidate as JSON, else the first JSON value inside it, else the error."""
    try:
        return _DECODER.decode(candidate), None
    except ValueError as exc:
        whole = exc
    starts = [i for i, ch in enumerate(candidate) if ch in "{["][:MAX_JSON_STARTS]
    first: ValueError | None = None
    for start in starts:
        try:
            value, _ = _DECODER.raw_decode(candidate, start)
        except ValueError as exc:
            first = first or exc
            continue
        return value, None
    # The error at the first bracket says what is wrong with the JSON; the
    # whole-text error usually only says prose came first.
    return None, first or whole


def extract_json(text: str) -> Any:
    """The JSON value in a model's answer.

    Raises `ValueError` (a `json.JSONDecodeError` where the JSON is malformed)
    when there is none. NaN and Infinity are refused: they are not JSON, and
    a lenient parse would hand them on as floats.
    """
    candidates = _candidates(text)
    if not candidates:
        raise ValueError("the answer was empty")
    first_error: ValueError | None = None
    for candidate in candidates:
        value, error = _decode(candidate)
        if error is None:
            return value
        first_error = first_error or error
    raise first_error or ValueError("the answer holds no JSON")


def _validation_feedback(exc: ValidationError) -> str:
    """Each error's field and message, which say what to fix. Never the input value."""
    problems = [
        f"- {'.'.join(str(part) for part in error['loc']) or '(root)'}: {error['msg']}"
        for error in exc.errors(include_url=False)[:MAX_FEEDBACK_ERRORS]
    ]
    return "It did not validate against the schema:\n" + "\n".join(problems)


def _instruction(response_model: type[BaseModel]) -> str:
    schema = json.dumps(response_model.model_json_schema())
    return (
        "Respond with JSON only, no prose and no code fence. "
        f"It must validate against this JSON schema: {schema}"
    )


def _retry_prompt(prompt: str, answer: str, feedback: str) -> str:
    echoed = answer.strip()[:MAX_ECHOED_CHARS]
    return (
        f"{prompt}\n\nYour previous answer:\n{echoed}\n\n{feedback}\n\n"
        "Reply again with only the corrected JSON."
    )


async def complete_structured[T: BaseModel](
    provider: LLMProvider,
    prompt: str,
    response_model: type[T],
    *,
    system: str | None = None,
    max_retries: int = 2,
    temperature: float = 0.0,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> T:
    """An instance of `response_model` from `provider`, asking again on a bad answer.

    At most `max_retries + 1` calls. Each retry repeats the original prompt
    with the previous answer and its errors, rather than growing a
    transcript, so the prompt stays one answer long.
    """
    if max_retries < 0:
        raise ValueError("max_retries cannot be negative")
    instruction = _instruction(response_model)
    system_text = f"{system}\n\n{instruction}" if system else instruction

    errors: list[str] = []
    user = prompt
    for attempt in range(max_retries + 1):
        answer = await provider.complete(
            system_text, user, max_tokens=max_tokens, temperature=temperature
        )
        try:
            return response_model.model_validate(extract_json(answer))
        except ValidationError as exc:  # before ValueError, which it subclasses
            kind, feedback = "validation", _validation_feedback(exc)
        except ValueError as exc:
            kind, feedback = "json", f"It was not valid JSON: {exc}"
        errors.append(feedback)
        log.info(
            "structured_output_invalid",
            schema=response_model.__name__,
            attempt=attempt + 1,
            kind=kind,
        )
        user = _retry_prompt(prompt, answer, feedback)

    raise StructuredOutputError(response_model.__name__, tuple(errors))


__all__ = [
    "MAX_ECHOED_CHARS",
    "StructuredOutputError",
    "complete_structured",
    "extract_json",
]
