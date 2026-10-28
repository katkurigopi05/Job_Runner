"""IDE connectivity is not model readiness; no private prompt precedes preflight."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import SecretStr

from packages.llm import kaggle
from packages.llm.provider import LLMError

SERVER = "https://kkb-production.jupyter-proxy.kaggle.net/k/123/synthetic-credential/proxy"
KERNEL = UUID("00000000-0000-4000-8000-000000000001")


def connection(tmp_path: Path) -> Path:
    path = tmp_path / "connection.json"
    path.write_text(json.dumps({"server_url": SERVER, "kernel_id": str(KERNEL)}))
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("state", ["helper_missing", "helper_outdated", "model_not_ready"])
async def test_private_prompt_is_not_sent_until_model_is_ready(tmp_path, monkeypatch, state):
    provider = kaggle.KaggleProvider(connection_file=connection(tmp_path))
    exchange = AsyncMock(return_value={"state": state})
    monkeypatch.setattr(provider, "_exchange", exchange)
    with pytest.raises(LLMError):
        await provider.complete("PRIVATE SYSTEM", "PRIVATE USER")
    assert exchange.await_count == 1
    expression = exchange.call_args.args[0]
    assert "PRIVATE" not in expression
    assert "jobrunner_health" in expression


async def test_ready_preflight_precedes_generation(tmp_path, monkeypatch):
    provider = kaggle.KaggleProvider(connection_file=connection(tmp_path))
    exchange = AsyncMock(
        side_effect=[
            {"state": "ready", "model": kaggle.MODEL, "protocol": 1, "context_tokens": 4096},
            {"model": kaggle.MODEL, "status": "ok", "text": "answer"},
        ]
    )
    monkeypatch.setattr(provider, "_exchange", exchange)
    assert await provider.complete("system", "private payload") == "answer"
    assert exchange.await_count == 2
    assert "private payload" not in exchange.call_args_list[0].args[0]
    assert "private payload" in exchange.call_args_list[1].args[0]


async def test_connected_wrong_model_is_not_ready(tmp_path, monkeypatch):
    provider = kaggle.KaggleProvider(connection_file=connection(tmp_path))
    monkeypatch.setattr(
        provider,
        "_exchange",
        AsyncMock(
            return_value={
                "state": "ready",
                "model": "different-model",
                "protocol": 1,
                "context_tokens": 4096,
            }
        ),
    )
    status = await provider.health()
    assert status.state == "model_not_ready"
    assert status.model is None


def test_connection_path_uses_selected_env_directory(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from packages.core import config

    root = tmp_path / "installation"
    root.mkdir()
    env = root / ".env"
    env.write_text("")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("JOBRUNNER_ENV_FILE", str(env))
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(kaggle_connection_file=Path("storage/connection.json")),
    )
    assert kaggle.connection_path() == root / "storage/connection.json"


async def test_status_never_echoes_connection_secret(tmp_path, monkeypatch):
    from packages.llm import kaggle_connection as manager

    path = connection(tmp_path)
    monkeypatch.setattr(manager, "connection_path", lambda: path)
    monkeypatch.setattr(manager, "discover_kernels", lambda _: [KERNEL])
    monkeypatch.setattr(
        kaggle.KaggleProvider,
        "health",
        AsyncMock(
            return_value=kaggle.KaggleStatus(
                state="ready",
                detail="Ready",
                kernel_id=KERNEL,
                model=kaggle.MODEL,
                context_tokens=4096,
            )
        ),
    )
    status = await manager.saved_status()
    assert status.state == "ready"
    assert "synthetic-credential" not in status.model_dump_json()


async def test_failed_reconnect_preserves_previous_connection(tmp_path, monkeypatch):
    from packages.llm import kaggle_connection as manager

    path = connection(tmp_path)
    previous = path.read_bytes()
    monkeypatch.setattr(manager, "connection_path", lambda: path)

    def failed(_):
        raise LLMError("Kaggle session expired.")

    monkeypatch.setattr(manager, "discover_kernels", failed)
    status = await manager.connect_session(SecretStr(SERVER))
    assert status.state != "ready"
    assert path.read_bytes() == previous


@pytest.mark.parametrize(
    "payload",
    [
        {"state": ["ready"]},
        {"state": "ready", "protocol": True},
        {"state": "ready", "model": kaggle.MODEL, "protocol": 1, "context_tokens": 8192},
    ],
)
async def test_malformed_health_never_counts_as_ready(tmp_path, monkeypatch, payload):
    provider = kaggle.KaggleProvider(connection_file=connection(tmp_path))
    monkeypatch.setattr(provider, "_exchange", AsyncMock(return_value=payload))
    assert (await provider.health()).state == "model_not_ready"


async def test_reconnect_saves_reachable_kernel_without_claiming_loaded_model(
    tmp_path, monkeypatch
):
    from packages.llm import kaggle_connection as manager

    path = tmp_path / "connection.json"
    monkeypatch.setattr(manager, "connection_path", lambda: path)
    monkeypatch.setattr(manager, "discover_kernels", lambda _: [KERNEL])
    monkeypatch.setattr(
        kaggle.KaggleProvider,
        "health",
        AsyncMock(
            return_value=kaggle.KaggleStatus(
                state="helper_missing",
                detail=kaggle.DETAILS["helper_missing"],
                kernel_id=KERNEL,
            )
        ),
    )
    status = await manager.connect_session(SecretStr(SERVER))
    assert status.state == "helper_missing"
    assert status.model is None
    assert kaggle.read_connection(path).kernel_id == KERNEL
    assert path.stat().st_mode & 0o777 == 0o600


async def test_reconnect_never_guesses_between_multiple_kernels(tmp_path, monkeypatch):
    from packages.llm import kaggle_connection as manager

    path = tmp_path / "connection.json"
    monkeypatch.setattr(manager, "connection_path", lambda: path)
    other = UUID(int=9)
    monkeypatch.setattr(manager, "discover_kernels", lambda _: [KERNEL, other])
    health = AsyncMock(return_value=kaggle.KaggleStatus(state="helper_missing", detail="Missing"))
    monkeypatch.setattr(kaggle.KaggleProvider, "health", health)
    status = await manager.connect_session(SecretStr(SERVER))
    assert status.state == "select_kernel"
    assert status.kernels == [KERNEL, other]
    assert not path.exists()
    assert health.await_count == 2
    assert [option.label for option in status.kernel_options] == [
        "Notebook 1 — no model helper loaded",
        "Notebook 2 — no model helper loaded",
    ]


async def test_reconnect_auto_selects_only_ready_notebook(tmp_path, monkeypatch):
    from packages.llm import kaggle_connection as manager

    path = tmp_path / "connection.json"
    other = UUID(int=9)
    monkeypatch.setattr(manager, "connection_path", lambda: path)
    monkeypatch.setattr(manager, "discover_kernels", lambda _: [KERNEL, other])

    async def health(provider):
        ready = provider.connection.kernel_id == other
        return kaggle.KaggleStatus(
            state="ready" if ready else "helper_missing",
            detail="Checked",
            model=kaggle.QWEN38_MODEL if ready else None,
        )

    monkeypatch.setattr(kaggle.KaggleProvider, "health", health)
    status = await manager.connect_session(SecretStr(SERVER))
    assert status.state == "ready" and status.model == kaggle.QWEN38_MODEL
    assert kaggle.read_connection(path).kernel_id == other
    assert status.kernel_options[1].label == "Notebook 2 — Qwen3.8 27B Q6_K"


async def test_two_ready_notebooks_show_models_without_guessing(tmp_path, monkeypatch):
    from packages.llm import kaggle_connection as manager

    path = tmp_path / "connection.json"
    monkeypatch.setattr(manager, "connection_path", lambda: path)
    monkeypatch.setattr(manager, "discover_kernels", lambda _: [KERNEL, UUID(int=9)])
    monkeypatch.setattr(
        kaggle.KaggleProvider,
        "health",
        AsyncMock(
            return_value=kaggle.KaggleStatus(
                state="ready",
                detail="Ready",
                model=kaggle.QWEN38_MODEL,
            )
        ),
    )
    status = await manager.connect_session(SecretStr(SERVER))
    assert status.state == "select_kernel" and not path.exists()
    assert all("Qwen3.8 27B" in option.label for option in status.kernel_options)
    assert all(str(option.id) not in option.label for option in status.kernel_options)


async def test_saving_rejects_foreign_kernel_and_failed_health(tmp_path, monkeypatch):
    from packages.llm import kaggle_connection as manager

    path = connection(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(manager, "connection_path", lambda: path)
    monkeypatch.setattr(manager, "discover_kernels", lambda _: [KERNEL])
    status = await manager.connect_session(SecretStr(SERVER), UUID(int=9))
    assert status.state == "kernel_missing"
    assert path.read_bytes() == before
    monkeypatch.setattr(
        kaggle.KaggleProvider,
        "health",
        AsyncMock(
            return_value=kaggle.KaggleStatus(
                state="unreachable",
                detail=kaggle.DETAILS["unreachable"],
            )
        ),
    )
    status = await manager.connect_session(SecretStr(SERVER), KERNEL)
    assert status.state == "unreachable"
    assert path.read_bytes() == before


async def test_status_detects_restarted_kernel_without_sending_prompt(tmp_path, monkeypatch):
    from packages.llm import kaggle_connection as manager

    path = connection(tmp_path)
    monkeypatch.setattr(manager, "connection_path", lambda: path)
    monkeypatch.setattr(manager, "discover_kernels", lambda _: [UUID(int=9)])
    health = AsyncMock()
    monkeypatch.setattr(kaggle.KaggleProvider, "health", health)
    assert (await manager.saved_status()).state == "kernel_missing"
    health.assert_not_awaited()


async def test_connection_endpoints_never_return_or_log_credentials(client, monkeypatch, caplog):
    from packages.llm import kaggle_connection as manager

    status = kaggle.KaggleStatus(
        state="ready", detail="Ready", kernel_id=KERNEL, model=kaggle.MODEL, context_tokens=4096
    )
    connect = AsyncMock(return_value=status)
    monkeypatch.setattr(manager, "connect_session", connect)
    monkeypatch.setattr(manager, "saved_status", AsyncMock(return_value=status))
    saved = await client.post("/chat/kaggle/connect", json={"server_url": SERVER})
    checked = await client.get("/chat/kaggle/status")
    assert saved.status_code == checked.status_code == 200
    assert saved.json()["state"] == checked.json()["state"] == "ready"
    assert "synthetic-credential" not in saved.text + checked.text + caplog.text
    assert connect.call_args.args[0].get_secret_value() == SERVER


async def test_invalid_destination_is_refused_before_any_network(client, monkeypatch):
    from packages.llm import kaggle_connection as manager

    discovery = AsyncMock()
    monkeypatch.setattr(manager, "discover_kernels", discovery)
    result = await client.post(
        "/chat/kaggle/connect", json={"server_url": "https://example.test/private-token"}
    )
    assert result.status_code == 400
    assert "private-token" not in result.text
    discovery.assert_not_called()


def test_notebook_helper_health_does_not_generate_or_load_a_model(monkeypatch):
    import sys
    from types import SimpleNamespace

    from scripts.kaggle_helper import install

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace())
    model = SimpleNamespace(
        is_loaded_in_4bit=True,
        device=SimpleNamespace(type="cuda"),
        config=SimpleNamespace(model_type="qwen3", hidden_size=4096, num_hidden_layers=36),
        generate=lambda **_: pytest.fail("Health must never generate"),
    )

    class Tokenizer:
        eos_token_id = 1

        def __call__(self, *args, **kwargs):
            pytest.fail("Health must never tokenize")

        apply_chat_template = __call__
        decode = __call__

    tokenizer = Tokenizer()
    helper = install(model, tokenizer)
    status = helper.jobrunner_health()
    assert status == {
        "state": "ready",
        "model": kaggle.MODEL,
        "protocol": 1,
        "context_tokens": 4096,
    }
    model.device.type = "cpu"
    assert helper.jobrunner_health()["state"] == "model_not_ready"
    model.device.type = "cuda"
    assert install(model, None).jobrunner_health()["state"] == "model_not_ready"
    model.generate = None
    assert helper.jobrunner_health()["state"] == "model_not_ready"


def test_default_connection_path_is_stable_across_launch_directories(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from packages.core import config

    monkeypatch.delenv("JOBRUNNER_ENV_FILE", raising=False)
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(kaggle_connection_file=Path("storage/kaggle-connection.json")),
    )
    expected = Path(kaggle.__file__).resolve().parents[2] / "storage/kaggle-connection.json"
    assert kaggle.connection_path() == expected
    monkeypatch.chdir(tmp_path)
    assert kaggle.connection_path() == expected
