"""OpenRouter, and the boundary it is deliberately kept behind.

One key here reaches many upstream models. That is the appeal, and it is also
why the provider is wired the way it is: §2.8 permits a single third-party
upload — the tailoring call — and asks that it be auditable. OpenRouter forwards
to an upstream the trail cannot see, and on a cloaked `stealth/*` route the
vendor is undisclosed by design. So the route has to be *chosen*, never
inherited from a key happening to be present.

The rest is the failure modes a reasoning model on a free route actually has:
an empty answer after the token budget went on thinking, a 401 carrying the
bearer token into the log, and a pre-release model id that disappears.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel

from packages.llm import router
from packages.llm.provider import (
    REASONING_HEADROOM_TOKENS,
    LLMError,
    OpenRouterProvider,
    build_provider,
)


class DummySchema(BaseModel):
    hello: str


def _response(payload: dict, *, status: int = 200) -> object:
    class MockResponse:
        status_code = status
        headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            if status >= 400:
                raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]

        def json(self) -> dict:
            return payload

    return MockResponse()


def _serves(monkeypatch, payload: dict, *, status: int = 200) -> dict:
    """Answer every POST with `payload`, and hand back what was sent."""
    seen: dict = {}

    async def mock_post(self, url, **kwargs):  # noqa: ANN001
        seen["url"] = url
        seen["json"] = kwargs.get("json")
        seen["headers"] = kwargs.get("headers")
        return _response(payload, status=status)

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    return seen


def _text(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def test_a_key_alone_does_not_change_what_auto_picks(monkeypatch) -> None:
    """The §2.8 boundary, and the reason this provider is not in QUALITY_ORDER.

    A key in `.env` must not silently redirect every "auto" task to a model
    whose upstream vendor the audit trail cannot name.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert "openrouter" not in router.QUALITY_ORDER
    assert router.best_available() != "openrouter"


def test_but_it_is_configured_so_it_can_be_asked_for_by_name(monkeypatch) -> None:
    """Not in the automatic order is not the same as unavailable."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    assert router._configured("openrouter") is True
    assert isinstance(build_provider("openrouter"), OpenRouterProvider)


def test_it_is_choosable_for_the_three_uploading_tasks(monkeypatch) -> None:
    """`LLM_TASK_TAILOR=openrouter` has to actually route there.

    The locked tasks are unaffected — that is `test_llm_router.py`'s job, and
    this only checks the choosable half reaches the new provider.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("LLM_TASK_TAILOR", "openrouter")
    monkeypatch.setenv("LLM_FALLBACK_LOCAL", "false")
    from packages.core.config import get_settings

    get_settings.cache_clear()
    try:
        assert router._chosen("tailor_resume") == "openrouter"
        assert isinstance(router.tailor_resume(), OpenRouterProvider)
    finally:
        get_settings.cache_clear()


def test_a_missing_key_says_which_variable(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from packages.core.config import get_settings

    get_settings.cache_clear()
    try:
        with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
            OpenRouterProvider()
    finally:
        get_settings.cache_clear()


def test_the_model_defaults_to_the_route_that_was_asked_for(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)

    assert OpenRouterProvider().model == "stealth/ox-alpha"


def test_the_model_is_overridable_because_stealth_routes_vanish(monkeypatch) -> None:
    """A pre-release route is withdrawn without notice and every call 404s."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct")

    assert OpenRouterProvider().model == "meta-llama/llama-3.3-70b-instruct"


@pytest.mark.asyncio
async def test_the_call_is_audited_as_having_left_the_machine(capsys, monkeypatch) -> None:
    """§2.8 wants proof of what left. The trail must not read like a local call."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _serves(monkeypatch, _text("tailored bullet"))

    answer = await OpenRouterProvider().complete("sys", "usr")

    assert answer == "tailored bullet"
    out = capsys.readouterr().out
    assert "llm_call" in out
    assert "provider=openrouter" in out

    from packages.llm.audit import is_local

    assert is_local("openrouter", "stealth/ox-alpha") is False


@pytest.mark.asyncio
async def test_it_sends_a_bearer_token_and_no_leaderboard_headers(monkeypatch) -> None:
    """HTTP-Referer and X-Title list the app publicly. §1 is a private tool."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    seen = _serves(monkeypatch, _text("ok"))

    await OpenRouterProvider().complete("sys", "usr")

    assert seen["headers"]["Authorization"] == "Bearer sk-or-test"
    assert "HTTP-Referer" not in seen["headers"]
    assert "X-Title" not in seen["headers"]
    assert seen["url"].endswith("/chat/completions")


@pytest.mark.asyncio
async def test_the_budget_leaves_room_for_reasoning_that_cannot_be_disabled(monkeypatch) -> None:
    """The bug this constant exists for, measured against the live route.

    `max_tokens` on this endpoint bounds reasoning *plus* answer, while every
    caller in this repo means it as an answer budget — `tailor_bullets` passes
    300 to keep a bullet bullet-sized. Passed through unchanged, a 300-token
    call came back `finish_reason="length"` with empty content: the whole
    allowance went on thinking and the model was cut off before writing. The
    tailorer then kept the original line, so the symptom was a tailorer that
    appeared to do nothing.

    Both ways of turning reasoning off are refused by the endpoint with
    "Reasoning is mandatory for this endpoint and cannot be disabled", so
    headroom is the only lever there is.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    seen = _serves(monkeypatch, _text("ok"))

    await OpenRouterProvider().complete("sys", "usr", max_tokens=300)

    assert seen["json"]["max_tokens"] == 300 + REASONING_HEADROOM_TOKENS
    # Excluding the trace does not stop it counting against the budget — it was
    # worth 299 completion tokens against a 300 cap — but it does cut it down.
    assert seen["json"]["reasoning"] == {"exclude": True}


@pytest.mark.asyncio
async def test_a_truncated_answer_says_it_was_truncated(monkeypatch) -> None:
    """An empty 200 is the shape this failure arrives in. Name the cause.

    Left to a generic message the owner has no way to tell "the model refused"
    from "the model was cut off mid-thought", and only the second one is fixed
    by raising the budget.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _serves(
        monkeypatch,
        {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]},
    )

    with pytest.raises(LLMError, match="cut off at the token limit"):
        await OpenRouterProvider().complete("sys", "usr")


@pytest.mark.asyncio
async def test_an_empty_answer_that_was_not_truncated_says_so_too(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _serves(
        monkeypatch,
        {
            "choices": [
                {"message": {"content": "", "reasoning_details": [{}]}, "finish_reason": "stop"}
            ]
        },
    )

    with pytest.raises(LLMError, match="returned no text"):
        await OpenRouterProvider().complete("sys", "usr")


@pytest.mark.asyncio
async def test_no_choices_is_reported_rather_than_an_indexerror(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _serves(monkeypatch, {"choices": []})

    with pytest.raises(LLMError, match="returned no choices"):
        await OpenRouterProvider().complete("sys", "usr")


@pytest.mark.asyncio
async def test_the_key_never_reaches_the_error_text(monkeypatch) -> None:
    """§2.7: secrets never appear in logs, and error paths are not exempt."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-supersecret-value")

    async def mock_post(self, url, **kwargs):  # noqa: ANN001
        raise RuntimeError(f"401 with headers Authorization: {kwargs['headers']['Authorization']}")

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    with pytest.raises(LLMError) as caught:
        await OpenRouterProvider().complete("sys", "usr")

    assert "supersecret" not in str(caught.value)


@pytest.mark.asyncio
async def test_json_mode_asks_for_json_both_ways(monkeypatch) -> None:
    """Not every model behind this endpoint honours `response_format`.

    One that ignores it returns prose that fails validation with no hint why,
    so the schema goes into the prompt as well.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    seen = _serves(monkeypatch, _text('{"hello": "world"}'))

    answer = await OpenRouterProvider().complete_json("sys", "usr", DummySchema)

    assert answer.hello == "world"
    assert seen["json"]["response_format"] == {"type": "json_object"}
    assert "schema" in seen["json"]["messages"][0]["content"].lower()
    # §7 pins JSON calls to 0.0 regardless of task — the answer has to parse.
    assert seen["json"]["temperature"] == 0.0
