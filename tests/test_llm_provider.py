"""Tests for LLM providers and audit logging."""

import pytest
from pydantic import BaseModel

from packages.core.config import Settings
from packages.llm.provider import (
    AnthropicProvider,
    GeminiProvider,
    LLMError,
    OllamaProvider,
    StubProvider,
    build_provider,
)


class DummySchema(BaseModel):
    hello: str


@pytest.mark.asyncio
async def test_stub_provider_completes():
    provider = StubProvider({"test": "it works"})
    ans = await provider.complete("system", "test")
    assert ans == "it works"


@pytest.mark.asyncio
async def test_stub_provider_json():
    provider = StubProvider({"test": '{"hello": "world"}'})
    ans = await provider.complete_json("system", "test", DummySchema)
    assert ans.hello == "world"


def test_build_provider_unknown():
    with pytest.raises(LLMError, match="is unknown or not implemented"):
        build_provider("invalid_provider")


def test_build_provider_unset(monkeypatch):
    import packages.core.config

    # Mock settings to return empty llm_provider
    monkeypatch.setattr(
        packages.core.config,
        "get_settings",
        lambda: Settings(llm_provider="", database_url="postgresql+asyncpg://foo/bar"),
    )
    with pytest.raises(LLMError, match="LLM_PROVIDER is unset or empty"):
        build_provider()


def test_build_provider_known(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    assert isinstance(build_provider("stub"), StubProvider)
    assert isinstance(build_provider("ollama"), OllamaProvider)
    assert isinstance(build_provider("gemini"), GeminiProvider)
    assert isinstance(build_provider("anthropic"), AnthropicProvider)


@pytest.mark.asyncio
async def test_the_prompt_never_reaches_the_logs(capsys) -> None:
    """§10 — logs must not carry résumé contents.

    This test previously asserted the opposite: that the prompt appeared in the
    log line. That satisfied §2.8's "log the call" by writing the owner's
    résumé into rotating log files, which is a copy nobody chose to make.
    The call is still recorded — see packages/llm/audit.py — by digest.
    """
    provider = StubProvider({"test": "hello"})

    await provider.complete("system_text", "user_test")

    out = capsys.readouterr().out
    assert "llm_call" in out
    assert "provider=stub" in out
    # The record exists...
    assert "user_sha256=" in out
    # ...and the text does not.
    assert "system_text" not in out
    assert "user_test" not in out


@pytest.mark.asyncio
async def test_audit_logging_in_ollama(capsys, monkeypatch):
    provider = OllamaProvider("http://test")

    import httpx

    class MockResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "ok"}}

    async def mock_post(*args, **kwargs):
        return MockResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    await provider.complete("sys", "usr")
    out = capsys.readouterr().out
    assert "llm_call" in out
    assert "provider=ollama" in out


@pytest.mark.asyncio
async def test_audit_logging_in_gemini(capsys, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    provider = GeminiProvider()

    import httpx

    class MockResponse:
        # status_code and headers exist because the provider now checks for a
        # 429 before reading the body, and reads Retry-After when it finds one.
        status_code = 200
        headers: dict[str, str] = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

    async def mock_post(*args, **kwargs):
        return MockResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    await provider.complete("sys", "usr")
    out = capsys.readouterr().out
    assert "llm_call" in out
    assert "provider=gemini" in out


@pytest.mark.asyncio
async def test_audit_logging_in_anthropic(capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    provider = AnthropicProvider()

    import httpx

    class MockResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"text": "ok"}]}

    async def mock_post(*args, **kwargs):
        return MockResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    await provider.complete("sys", "usr")
    out = capsys.readouterr().out
    assert "llm_call" in out
    assert "provider=anthropic" in out
