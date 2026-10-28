"""Kaggle is an explicit remote choice; notebook credentials stay private."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from packages.llm import kaggle, router
from packages.llm.audit import is_local, read_trail
from packages.llm.provider import LLMError, PromptTooLong, StubProvider, build_provider

MODEL = "unsloth/Qwen3-8B-bnb-4bit"
SECRET = "synthetic-session-credential"
SERVER = f"https://kkb-production.jupyter-proxy.kaggle.net/k/123/{SECRET}/proxy"
KERNEL = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def connection(tmp_path: Path) -> Path:
    path = tmp_path / "connection.json"
    path.write_text(json.dumps({"server_url": SERVER, "kernel_id": KERNEL}))
    path.chmod(0o600)
    return path


def test_connection_is_required_and_errors_do_not_quote_it(tmp_path: Path) -> None:
    with pytest.raises(LLMError, match="Kaggle connection") as raised:
        kaggle.KaggleProvider(connection_file=tmp_path / SECRET)
    assert SECRET not in str(raised.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8888",
        SERVER + "/../evil",
        "https://evil.example/",
        SERVER + "?token=secret",
    ],
)
def test_only_the_authenticated_kaggle_url_is_accepted(connection: Path, url: str) -> None:
    connection.write_text(json.dumps({"server_url": url, "kernel_id": KERNEL}))
    with pytest.raises(LLMError):
        kaggle.KaggleProvider(connection_file=connection)


def test_connection_file_must_be_private(connection: Path) -> None:
    connection.chmod(0o644)
    with pytest.raises(LLMError, match="0600"):
        kaggle.KaggleProvider(connection_file=connection)


def test_credentials_do_not_appear_in_representations(connection: Path) -> None:
    provider = kaggle.KaggleProvider(connection_file=connection)
    assert SECRET not in repr(provider)
    assert SECRET not in repr(provider.connection)
    assert is_local(provider.name, provider.model) is False
    assert "kaggle" not in router.QUALITY_ORDER


class Kernel:
    def __init__(self, result: dict | None = None, failure: str | None = None) -> None:
        self.result = result or {"model": MODEL, "text": "3 jobs", "status": "ok"}
        self.failure = failure
        self.sent: dict = {}

    async def send(self, value: str) -> None:
        self.sent = json.loads(value)

    async def recv(self) -> str:
        if self.failure:
            raise OSError(self.failure)
        result = (
            {"state": "ready", "model": MODEL, "protocol": 1, "context_tokens": 4096}
            if "jobrunner_health" in self.sent["content"]["user_expressions"]["jobrunner"]
            else self.result
        )
        return json.dumps(
            {
                "msg_type": "execute_reply",
                "parent_header": {"msg_id": self.sent["header"]["msg_id"]},
                "content": {
                    "status": "ok",
                    "user_expressions": {
                        "jobrunner": {
                            "status": "ok",
                            "data": {"text/plain": repr(json.dumps(result))},
                        }
                    },
                },
            }
        )


def serves(monkeypatch, kernel: Kernel) -> dict:
    from contextlib import asynccontextmanager

    seen: dict = {}

    @asynccontextmanager
    async def connect(url, **kwargs):
        seen.update(url=url, **kwargs)
        yield kernel

    monkeypatch.setattr(kaggle, "SessionConnection", connect)
    return seen


async def test_prompt_is_data_not_executable_code_and_history_is_off(
    connection: Path, monkeypatch
) -> None:
    kernel = Kernel()
    seen = serves(monkeypatch, kernel)
    provider = kaggle.KaggleProvider(connection_file=connection)
    hostile = "'); __import__('os').system('bad'); #\n\\quoted"
    assert await provider.complete("system", hostile, max_tokens=600, temperature=0.2) == "3 jobs"
    assert seen["url"] == SERVER.replace("https://", "wss://") + f"/api/kernels/{KERNEL}/channels"
    assert seen["logger"].disabled
    assert seen["proxy"] is None
    content = kernel.sent["content"]
    assert content["code"] == ""
    assert content["silent"] and not content["store_history"] and not content["allow_stdin"]
    expression = ast.parse(content["user_expressions"]["jobrunner"], mode="eval").body
    assert isinstance(expression, ast.Call)
    assert isinstance(expression.func, ast.Name) and expression.func.id == "jobrunner_complete"
    payload = json.loads(ast.literal_eval(expression.args[0]))
    assert payload == {
        "model": MODEL,
        "system": "system",
        "user": hostile,
        "max_tokens": 600,
        "temperature": 0.2,
    }
    audit = read_trail()
    assert audit[-1].provider == "kaggle" and audit[-1].left_machine
    assert SECRET not in repr(audit)


def test_session_connection_never_follows_redirects() -> None:
    connection = kaggle.SessionConnection("wss://example.invalid")
    failure = OSError("expired session")
    assert connection.process_redirect(failure) is failure


@pytest.mark.parametrize(
    "result,error",
    [
        ({"status": "too_long", "model": MODEL}, PromptTooLong),
        ({"status": "ok", "model": "other", "text": "answer"}, LLMError),
        ({"status": "ok", "model": MODEL, "text": ""}, LLMError),
        ({"status": "not_ready", "model": MODEL}, LLMError),
    ],
)
async def test_bad_or_missing_model_never_returns_an_answer(
    connection: Path, monkeypatch, result: dict, error: type[Exception]
) -> None:
    serves(monkeypatch, Kernel(result))
    with pytest.raises(error):
        await kaggle.KaggleProvider(connection_file=connection).complete("system", "user")


async def test_transport_failures_never_expose_url_or_prompt(connection: Path, monkeypatch) -> None:
    serves(monkeypatch, Kernel(failure=f"failed {SERVER} private prompt"))
    with pytest.raises(LLMError) as raised:
        await kaggle.KaggleProvider(connection_file=connection).complete("system", "private prompt")
    assert SECRET not in str(raised.value) and "private prompt" not in str(raised.value)


def test_factory_uses_explicit_configuration(connection: Path, monkeypatch) -> None:
    from packages.core.config import get_settings

    monkeypatch.setenv("KAGGLE_CONNECTION_FILE", str(connection))
    get_settings.cache_clear()
    try:
        provider = build_provider("kaggle")
        assert provider.name == "kaggle" and provider.model is None  # resolved by live readiness
        assert str(provider.connection.kernel_id) == KERNEL
    finally:
        get_settings.cache_clear()


async def test_factory_masks_contacts_before_the_authenticated_kernel(
    connection, monkeypatch
) -> None:
    from packages.core.config import get_settings
    from packages.llm import privacy
    from packages.llm.provider import stream_text

    async def contacts():
        return privacy.Identifiers(names=("Alex Morgan",))

    monkeypatch.setattr(privacy, "load_identifiers", contacts)
    kernel = Kernel()
    serves(monkeypatch, kernel)
    monkeypatch.setenv("KAGGLE_CONNECTION_FILE", str(connection))
    get_settings.cache_clear()
    guarded = build_provider("kaggle")
    get_settings.cache_clear()
    assert isinstance(guarded, privacy.PrivacyProvider)
    prompt = "Alex Morgan, alex.morgan@example.net, +1 (202) 555-0147"
    assert (
        "".join([chunk async for chunk in stream_text(guarded, "Alex Morgan", prompt)]) == "3 jobs"
    )
    assert "Alex" not in json.dumps(kernel.sent) and "Morgan" not in json.dumps(kernel.sent)
    assert "example.net" not in json.dumps(kernel.sent) and "555-0147" not in json.dumps(
        kernel.sent
    )
    assert "JR_PRIVATE_" in json.dumps(kernel.sent)


async def test_chat_can_choose_kaggle_but_not_as_default(client, monkeypatch) -> None:
    import apps.api.routers.chat as chat

    stub = StubProvider({"": "A supported answer."})
    stub.model = MODEL
    selected: list[str] = []

    def build(name):
        selected.append(name)
        return stub

    monkeypatch.setattr(chat.llm_router, "build_provider", build)
    response = await client.post("/chat", json={"message": "hello", "provider": "kaggle"})
    assert response.status_code == 200
    assert response.json()["local"] is False
    assert response.json()["provider"] == "kaggle"
    assert selected == ["kaggle"]
    response = await client.post("/chat", json={"message": "hello"})
    assert response.status_code == 200 and selected[-1] == "ollama"


def test_setup_saves_the_real_url_privately_without_printing_it(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from types import SimpleNamespace

    from scripts import configure_kaggle

    destination = tmp_path / "storage" / "connection.json"
    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: SimpleNamespace(kaggle_connection_file=destination),
    )
    monkeypatch.setattr(configure_kaggle.getpass, "getpass", lambda _: SERVER)
    monkeypatch.setattr(configure_kaggle, "discover_kernels", lambda _: [kaggle.UUID(KERNEL)])
    monkeypatch.setattr("builtins.input", lambda _: KERNEL)
    configure_kaggle.main()
    assert destination.stat().st_mode & 0o777 == 0o600
    assert kaggle.read_connection(destination).server_url.get_secret_value() == SERVER
    assert SECRET not in capsys.readouterr().out


def test_notebook_helper_refuses_an_overlong_prompt_before_generating(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    from scripts.kaggle_helper import install

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace())
    model = SimpleNamespace(
        device=SimpleNamespace(type="cuda"),
        is_loaded_in_4bit=True,
        config=SimpleNamespace(model_type="qwen3", hidden_size=4096, num_hidden_layers=36),
    )

    class Tokenizer:
        def apply_chat_template(self, messages, **kwargs):
            assert kwargs["enable_thinking"] is False
            return "prompt"

        def __call__(self, prompt, **kwargs):
            return {"input_ids": SimpleNamespace(shape=(1, 4000))}

    helper = install(model, Tokenizer())
    result = json.loads(
        helper(
            json.dumps(
                {
                    "model": MODEL,
                    "system": "system",
                    "user": "user",
                    "max_tokens": 600,
                    "temperature": 0.2,
                }
            )
        )
    )
    assert result == {"model": MODEL, "status": "too_long"}
