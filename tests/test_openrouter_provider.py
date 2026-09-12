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
    """Unsetting the environment variable is not enough to unset the key.

    `Settings` reads `.env` as well as the environment — deliberately, because
    a key in the documented place must not read as unconfigured. So a test that
    only calls `delenv` passes on a machine with no `.env` key and fails the
    moment the owner adds one, which is backwards: it would go green in CI and
    red on the only machine that matters.

    Both sources are cleared here, so this asserts the provider's behaviour
    rather than the state of whoever's checkout it runs in.
    """
    import packages.core.config as core_config
    from packages.core.config import get_settings

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    get_settings.cache_clear()
    settings = get_settings().model_copy(update={"openrouter_api_key": None})
    monkeypatch.setattr(core_config, "get_settings", lambda: settings)

    try:
        with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
            OpenRouterProvider()
    finally:
        get_settings.cache_clear()


def test_the_model_defaults_to_the_route_that_was_asked_for(monkeypatch) -> None:
    """The built-in default, isolated from whatever this machine's `.env` says.

    Clearing the environment variable is not enough: `OpenRouterProvider` falls
    back to `settings.openrouter_model`, which pydantic reads from `.env` — so
    this passed only on a checkout whose `.env` happened to be silent about it,
    and started failing the moment one set the key it is documented to set.
    A test that asserts a default has to control every source of that default.
    """
    import packages.core.config as core_config
    from packages.core.config import get_settings

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    get_settings.cache_clear()
    settings = get_settings().model_copy(update={"openrouter_model": None})
    monkeypatch.setattr(core_config, "get_settings", lambda: settings)

    try:
        assert OpenRouterProvider().model == OpenRouterProvider.DEFAULT_MODEL
    finally:
        get_settings.cache_clear()


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


@pytest.mark.asyncio
async def test_a_withdrawn_route_names_the_one_line_fix(monkeypatch) -> None:
    """The failure `.env.example` predicts, and the owner actually hit.

    `stealth/ox-alpha` — the shipped default — was withdrawn, and every call
    404s from then on. Reported as a bare "404 Not Found" it reads like a bug in
    this code or a bad key, and the tailorer's own fallback turns it into a
    résumé that silently went untailored on a provider the owner believed was
    working. The remedy is one line in `.env`, so the error says which line.
    """
    from packages.llm.provider import LLMError, OpenRouterProvider

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    provider = OpenRouterProvider(model="stealth/ox-alpha")

    class Resp:
        status_code = 404
        headers: dict[str, str] = {}

        def json(self) -> dict[str, object]:
            return {}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, *args: object, **kwargs: object) -> Resp:
            return Resp()

    monkeypatch.setattr("packages.llm.provider.httpx.AsyncClient", lambda *a, **k: Client())

    with pytest.raises(LLMError) as caught:
        await provider.complete("s", "u", max_tokens=50)

    message = str(caught.value)
    assert "stealth/ox-alpha" in message
    assert "OPENROUTER_MODEL" in message


def test_the_shipped_default_is_not_a_route_that_vanishes() -> None:
    """The default was itself the class of route this module warns about.

    `DEFAULT_MODEL` was `stealth/ox-alpha`, and the docstring two lines above
    it says pre-release routes "get withdrawn without notice, at which point
    every call 404s". So the shipped default was guaranteed to break, and did:
    the owner hit it, and hit it again on `minimax/minimax-m3:free` after
    swapping to another pre-release route.

    A default cannot be guaranteed to survive — no free route on this gateway
    is contractual — but it can avoid the one category that is *designed* to be
    temporary. Measured on 2026-09-12, `nvidia/nemotron-3-super-120b-a12b:free`
    was the longest-standing free route OpenRouter served: added 2026-03-11,
    where the next oldest was three weeks younger and nine of the nineteen were
    added in the preceding two months.

    Two properties beyond age, both load-bearing:

    - **The vendor is nameable.** §2.8 permits one third-party upload of the
      résumé and the trail records where it went. A `stealth/*` route forwards
      to an upstream that is undisclosed by design, so the trail can record
      the hop but not the destination.
    - **The weights are open.** A single-vendor preview disappears when that
      vendor withdraws it; an open-weight model can be served by more than one
      provider, so the route outliving any one of them is at least possible.
    """
    from packages.llm.provider import OpenRouterProvider

    default = OpenRouterProvider.DEFAULT_MODEL

    assert not default.startswith("stealth/"), (
        "the default must not be a pre-release route — this module's own "
        "docstring says those are withdrawn without notice"
    )
    for marker in ("preview", "alpha", "beta", "experimental"):
        assert marker not in default.lower(), f"{default!r} reads as temporary"


def test_the_default_is_still_overridable() -> None:
    """Changing the default must not take the escape hatch with it."""
    import os

    from packages.llm.provider import OpenRouterProvider

    os.environ["OPENROUTER_API_KEY"] = "k"
    os.environ["OPENROUTER_MODEL"] = "some/other-route:free"
    try:
        assert OpenRouterProvider().model == "some/other-route:free"
    finally:
        os.environ.pop("OPENROUTER_MODEL", None)


def test_each_gateway_names_its_own_default_route() -> None:
    """The shared base must not hand one gateway the other's model.

    `_OpenAICompatibleProvider.DEFAULT_MODEL` held `stealth/ox-alpha` — an
    OpenRouter id — so `TokenRouterProvider` inherited it on any path that did
    not set `TOKENROUTER_MODEL`. It happens to define its own, so nothing broke;
    a third gateway added later would have dialled a withdrawn OpenRouter
    preview and reported it as its own 404.
    """
    from packages.llm.provider import (
        OpenRouterProvider,
        TokenRouterProvider,
        _OpenAICompatibleProvider,
    )

    assert "DEFAULT_MODEL" not in vars(_OpenAICompatibleProvider), (
        "the shared base must declare DEFAULT_MODEL, not supply one"
    )
    for provider in (OpenRouterProvider, TokenRouterProvider):
        assert "DEFAULT_MODEL" in vars(provider), f"{provider.__name__} must name its own"
