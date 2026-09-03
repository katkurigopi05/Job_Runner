"""TokenRouter — a second gateway behind the same boundary as the first.

It exists because its GLM route is genuinely free where OpenRouter's are not:
of eighteen `z-ai/*` ids there, exactly one prices at zero. Two free routes is
what makes §7's compare panel worth opening.

Everything that keeps OpenRouter behind a deliberate choice applies here
unchanged, and these tests hold it to that — a key must not redirect anything,
the provider must be reachable only by name, and the trail must say where the
résumé went. What is new is measured rather than assumed: reasoning is on and
billed against `max_tokens`, and the route is slow enough that the inherited
timeout was wrong.
"""

from __future__ import annotations

import httpx
import pytest

from packages.llm import router
from packages.llm.provider import (
    REASONING_HEADROOM_TOKENS,
    LLMError,
    OpenRouterProvider,
    TokenRouterProvider,
    _OpenAICompatibleProvider,
    _scrubbed,
    build_provider,
)


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
    seen: dict = {}

    async def mock_post(self, url, **kwargs):  # noqa: ANN001
        seen["url"] = url
        seen["json"] = kwargs.get("json")
        seen["headers"] = kwargs.get("headers")
        seen["timeout"] = kwargs.get("timeout")
        return _response(payload, status=status)

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    return seen


def _text(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


# --------------------------------------------------------------------------
# The boundary
# --------------------------------------------------------------------------


def test_a_key_alone_does_not_change_what_auto_picks(monkeypatch) -> None:
    """The §2.8 rule, restated for the second gateway.

    TokenRouter forwards to an upstream the trail cannot name, exactly as
    OpenRouter does. A key in `.env` must not make it the answer to anything.
    """
    monkeypatch.setenv("TOKENROUTER_API_KEY", "sk-tr-test")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert "tokenrouter" not in router.QUALITY_ORDER
    assert router.best_available() != "tokenrouter"


def test_it_is_reachable_by_name(monkeypatch) -> None:
    monkeypatch.setenv("TOKENROUTER_API_KEY", "sk-tr-test")

    assert router._configured("tokenrouter") is True
    assert isinstance(build_provider("tokenrouter"), TokenRouterProvider)


def test_it_can_be_the_remote_half_of_a_comparison(monkeypatch) -> None:
    """The panel is the point: a free route deserves a look before adoption."""
    monkeypatch.setenv("TOKENROUTER_API_KEY", "sk-tr-test")

    assert router.is_comparable_cloud("tokenrouter") is True
    assert "tokenrouter" in router.comparable_clouds()


def test_without_a_key_it_is_neither_configured_nor_offered(monkeypatch) -> None:
    monkeypatch.delenv("TOKENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: type("S", (), {"tokenrouter_api_key": None})(),
    )

    assert router._configured("tokenrouter") is False


def test_the_base_class_is_not_selectable() -> None:
    """`_OpenAICompatibleProvider` is machinery, not a destination.

    Its `name` is empty and `build_provider` has no branch for it, so nothing
    can route a résumé to an endpoint that was never named.
    """
    assert _OpenAICompatibleProvider.name == ""
    with pytest.raises(LLMError, match="unknown or not implemented"):
        build_provider("_openaicompatibleprovider")


# --------------------------------------------------------------------------
# What the extraction must not have changed
# --------------------------------------------------------------------------


def test_the_two_gateways_keep_separate_identities() -> None:
    """Sharing the transport must not blur where a résumé went.

    §2.8 asks the trail to name the destination. A shared base with a settable
    base URL would make that "wherever `.env` last pointed", so the endpoint
    and the key are fixed per class.
    """
    assert OpenRouterProvider.BASE_URL != TokenRouterProvider.BASE_URL
    assert OpenRouterProvider.KEY_ENV != TokenRouterProvider.KEY_ENV
    assert OpenRouterProvider.name == "openrouter"
    assert TokenRouterProvider.name == "tokenrouter"


@pytest.mark.asyncio
async def test_it_posts_to_its_own_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("TOKENROUTER_API_KEY", "sk-tr-test")
    seen = _serves(monkeypatch, _text("Rewritten."))

    await TokenRouterProvider().complete("sys", "user", max_tokens=300)

    assert seen["url"] == "https://api.tokenrouter.com/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer sk-tr-test"


@pytest.mark.asyncio
async def test_the_reasoning_headroom_is_this_route_s_own(monkeypatch) -> None:
    """The inherited allowance was measured on the wrong prompt.

    An 89-character probe reasoned for 398 tokens, which made the shared 1024
    look ample. The real tailoring prompt is ~6k characters — the posting plus
    the supported and off-limits term lists — and on that this route spends
    **2,982 reasoning tokens to write a 48-token bullet**. At 1024 every real
    call returned `finish_reason="length"` with empty content and all three
    test bullets fell back to their originals: a tailorer that appears to do
    nothing, which is the failure the constant's own docstring warns about.

    So the headroom is per provider, and this asserts the route uses its own
    rather than the inherited one.
    """
    monkeypatch.setenv("TOKENROUTER_API_KEY", "sk-tr-test")
    seen = _serves(monkeypatch, _text("Rewritten."))

    await TokenRouterProvider().complete("sys", "user", max_tokens=300)

    assert TokenRouterProvider.REASONING_HEADROOM > REASONING_HEADROOM_TOKENS
    assert seen["json"]["max_tokens"] == 300 + TokenRouterProvider.REASONING_HEADROOM


def test_openrouter_keeps_the_allowance_it_was_measured_with() -> None:
    """The extraction must not have moved the other route's number."""
    assert OpenRouterProvider.REASONING_HEADROOM == REASONING_HEADROOM_TOKENS


@pytest.mark.asyncio
async def test_an_empty_answer_says_the_budget_ran_out(monkeypatch) -> None:
    """The failure that looks like a tailorer doing nothing."""
    monkeypatch.setenv("TOKENROUTER_API_KEY", "sk-tr-test")
    _serves(monkeypatch, {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})

    with pytest.raises(LLMError, match="REASONING_HEADROOM_TOKENS"):
        await TokenRouterProvider().complete("sys", "user", max_tokens=300)


@pytest.mark.asyncio
async def test_a_404_names_the_suffix_that_costs_money(monkeypatch) -> None:
    """`z-ai/glm-5.3` and `z-ai/glm-5.3-free` differ by one suffix."""
    monkeypatch.setenv("TOKENROUTER_API_KEY", "sk-tr-test")
    _serves(monkeypatch, {}, status=404)

    with pytest.raises(LLMError, match="-free"):
        await TokenRouterProvider().complete("sys", "user", max_tokens=300)


@pytest.mark.asyncio
async def test_it_waits_longer_than_the_default(monkeypatch) -> None:
    """A real bullet rewrite took 71s and the inherited 120s was not enough
    once the reasoning headroom was in play — the call failed as a read timeout
    *after* the upstream had already produced the answer."""
    monkeypatch.setenv("TOKENROUTER_API_KEY", "sk-tr-test")
    seen = _serves(monkeypatch, _text("Rewritten."))

    await TokenRouterProvider().complete("sys", "user", max_tokens=300)

    assert seen["timeout"] == TokenRouterProvider.REQUEST_TIMEOUT_S
    assert TokenRouterProvider.REQUEST_TIMEOUT_S > OpenRouterProvider.REQUEST_TIMEOUT_S


def test_a_silent_exception_still_names_itself() -> None:
    """`httpx.ReadTimeout("")` renders as nothing.

    Interpolated into "<service> call failed: {…}" that produced a message
    ending in a colon, which is what the first real timeout on this route
    actually logged.
    """
    assert _scrubbed(httpx.ReadTimeout("")) == "ReadTimeout"
    assert _scrubbed(ValueError("real message")) == "real message"
