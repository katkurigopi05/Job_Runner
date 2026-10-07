"""What the daemon measured about a call, kept beside the audit trail.

Ollama answers every chat call with how many tokens it read and wrote and how
long each took. The provider read `message.content` and dropped the rest, so
two things were measured by hand or not at all:

- **Reading against writing.** CLAUDE.md §14 found that MLX wrote faster than
  Ollama and read the prompt slower, and so was no faster end to end. That
  took a separate benchmark to see; the split is in every reply.
- **A prompt past the context cap.** §14 records that Ollama cuts one without
  an error. A context filled to the cap is the only sign of it.

The timings are their own file. The audit trail answers "what left this
machine" and the daily quota is counted from its lines, so a second line per
call there would halve every provider's allowance.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from packages.core.config import get_settings
from packages.llm import audit, timing
from packages.llm import provider as provider_module
from packages.llm.provider import OllamaCloudProvider, OllamaProvider

SYSTEM = "You answer briefly."
USER = "A question that names Northwind Aerospace, which must not be written down."

#: A reply shaped like the daemon's, durations in nanoseconds as it sends them.
MEASURED = {
    "total_duration": 17_400_000_000,
    "load_duration": 120_000_000,
    "prompt_eval_count": 1_312,
    "prompt_eval_duration": 5_050_000_000,
    "eval_count": 146,
    "eval_duration": 12_100_000_000,
}


class _Answer(BaseModel):
    value: str


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture
def daemon(monkeypatch):
    """Stands in for Ollama and answers with whatever measurements it is given."""

    def install(measurements: dict[str, Any] | None = None) -> None:
        async def post(self, url, *, json=None, headers=None, timeout=None):  # noqa: ANN001
            if url.endswith("/api/show"):
                return _Response({"capabilities": ["completion"]})
            content = '{"value": "ok"}' if json.get("format") == "json" else "an answer"
            return _Response({"message": {"content": content}, **(measurements or {})})

        monkeypatch.setattr(httpx.AsyncClient, "post", post)

    provider_module._THINKING_MODELS.clear()
    yield install
    provider_module._THINKING_MODELS.clear()


class _Log:
    """Records what would have been logged, by level and event name."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.events.append(("info", event, fields))

    def warning(self, event: str, **fields: Any) -> None:
        self.events.append(("warning", event, fields))

    def error(self, event: str, **fields: Any) -> None:
        self.events.append(("error", event, fields))

    def named(self, event: str) -> list[dict[str, Any]]:
        return [fields for _, name, fields in self.events if name == event]


@pytest.fixture
def logged(monkeypatch) -> _Log:
    log = _Log()
    monkeypatch.setattr(timing, "log", log)
    return log


def _local() -> OllamaProvider:
    return OllamaProvider("http://test", model="some-local-model")


async def test_a_local_call_keeps_what_the_daemon_measured(daemon) -> None:
    daemon(MEASURED)

    answer = await _local().complete(SYSTEM, USER, max_tokens=600)

    assert answer == "an answer"
    [kept] = timing.read_timings()
    assert (kept.provider, kept.model) == ("ollama", "some-local-model")
    assert (kept.prompt_tokens, kept.answer_tokens) == (1_312, 146)
    # Milliseconds: the daemon sends nanoseconds, and nobody reads those.
    assert (kept.load_ms, kept.prompt_ms, kept.answer_ms, kept.total_ms) == (
        120.0,
        5_050.0,
        12_100.0,
        17_400.0,
    )
    assert kept.context_limit == get_settings().ollama_num_ctx


async def test_reading_and_writing_are_told_apart(daemon) -> None:
    """The two numbers §14 needed a benchmark for."""
    daemon(MEASURED)

    await _local().complete(SYSTEM, USER)

    [kept] = timing.read_timings()
    assert kept.before_first_token_ms == 5_170.0, "loading plus reading the prompt"
    assert kept.answer_tokens_per_second == pytest.approx(146 / 12.1)
    assert kept.prompt_tokens_per_second == pytest.approx(1_312 / 5.05)


async def test_the_line_joins_to_the_audit_entry_for_the_same_call(daemon) -> None:
    daemon(MEASURED)

    await _local().complete(SYSTEM, USER)

    [entry] = audit.read_trail()
    [kept] = timing.read_timings()
    assert kept.user_sha256 == entry.user_sha256 == audit.digest_of(USER)


async def test_timings_are_not_lines_in_the_audit_trail(daemon) -> None:
    """The quota counts the trail's lines, one per call."""
    daemon(MEASURED)

    for _ in range(3):
        await _local().complete(SYSTEM, USER)

    assert len(audit.read_trail()) == 3
    assert len(timing.read_timings()) == 3
    assert timing.timings_path() != audit.audit_path()
    assert timing.timings_path().parent == audit.audit_path().parent


async def test_nothing_of_the_prompt_is_written_down(daemon) -> None:
    """§10. Counts, durations and a digest; the exact keys, so adding one is deliberate."""
    daemon(MEASURED)

    await _local().complete(SYSTEM, USER)

    written = timing.timings_path().read_text()
    assert "Northwind" not in written
    assert "briefly" not in written
    assert set(json.loads(written)) == {
        "at",
        "provider",
        "model",
        "user_sha256",
        "prompt_tokens",
        "answer_tokens",
        "load_ms",
        "prompt_ms",
        "answer_ms",
        "total_ms",
        "context_limit",
    }


async def test_a_context_filled_to_the_cap_is_flagged(daemon, logged) -> None:
    """Ollama cuts a prompt longer than the cap and says nothing."""
    cap = get_settings().ollama_num_ctx
    daemon({**MEASURED, "prompt_eval_count": cap - 100, "eval_count": 100})

    await _local().complete(SYSTEM, USER)

    [kept] = timing.read_timings()
    assert kept.context_full
    [warning] = logged.named("llm_context_full")
    assert warning["context_limit"] == cap
    assert warning["prompt_tokens"] == cap - 100


async def test_a_context_with_room_left_is_not_flagged(daemon, logged) -> None:
    daemon(MEASURED)

    await _local().complete(SYSTEM, USER)

    [kept] = timing.read_timings()
    assert not kept.context_full
    assert logged.named("llm_context_full") == []
    assert len(logged.named("llm_call_timing")) == 1


async def test_a_call_that_asks_for_json_is_measured_too(daemon) -> None:
    daemon(MEASURED)

    parsed = await _local().complete_json(SYSTEM, USER, _Answer)

    assert parsed.value == "ok"
    [kept] = timing.read_timings()
    assert kept.answer_tokens == 146


async def test_a_reply_with_no_measurements_keeps_nothing(daemon) -> None:
    """A proxy, or an older daemon. The answer is still the answer."""
    daemon(None)

    assert await _local().complete(SYSTEM, USER) == "an answer"
    assert timing.read_timings() == []


async def test_a_measurement_of_the_wrong_kind_is_left_out(daemon) -> None:
    daemon({**MEASURED, "eval_count": "many", "eval_duration": None, "load_duration": True})

    await _local().complete(SYSTEM, USER)

    [kept] = timing.read_timings()
    assert kept.answer_tokens is None
    assert kept.answer_ms is None
    assert kept.load_ms is None, "a boolean is not a duration"
    assert kept.prompt_tokens == 1_312
    assert kept.answer_tokens_per_second is None


async def test_a_file_that_cannot_be_written_does_not_fail_the_call(
    daemon, logged, monkeypatch
) -> None:
    daemon(MEASURED)

    def unwritable() -> Any:
        raise OSError("read-only volume at /somewhere/private")

    monkeypatch.setattr(timing, "timings_path", unwritable)

    assert await _local().complete(SYSTEM, USER) == "an answer"
    [failure] = logged.named("llm_timing_write_failed")
    assert failure == {"error": "OSError"}, "the type, never the message"


async def test_a_hosted_model_has_no_cap_of_ours_to_fill(daemon, logged) -> None:
    """`ollama_cloud` sends no `num_ctx`, so there is no limit to compare with."""
    daemon({**MEASURED, "prompt_eval_count": 50_000})

    await OllamaCloudProvider("http://test", model="glm-5.3-flash:cloud").complete(SYSTEM, USER)

    [kept] = timing.read_timings()
    assert kept.provider == "ollama_cloud"
    assert kept.context_limit is None
    assert not kept.context_full
    assert logged.named("llm_context_full") == []


def test_no_file_means_no_timings() -> None:
    assert timing.read_timings() == []


async def test_the_newest_lines_are_the_ones_returned(daemon) -> None:
    for tokens in (10, 20, 30):
        daemon({**MEASURED, "eval_count": tokens})
        await _local().complete(SYSTEM, USER)

    assert [kept.answer_tokens for kept in timing.read_timings(limit=2)] == [20, 30]
