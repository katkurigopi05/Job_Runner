"""A new Kaggle URL replaces stale routing without guessing a model kernel."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from packages.llm import kaggle
from packages.llm.provider import LLMError
from scripts import configure_kaggle

SERVER = "https://kkb-production.jupyter-proxy.kaggle.net/k/123/synthetic-credential/proxy"
KERNEL = UUID("00000000-0000-4000-8000-000000000001")
OTHER = UUID("00000000-0000-4000-8000-000000000002")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    destination = tmp_path / "storage" / "connection.json"
    monkeypatch.setattr(
        configure_kaggle,
        "get_settings",
        lambda: SimpleNamespace(kaggle_connection_file=destination),
    )
    monkeypatch.setattr(configure_kaggle.getpass, "getpass", lambda _: SERVER)
    return destination


def discover(monkeypatch, kernels):
    monkeypatch.setattr(configure_kaggle, "discover_kernels", lambda _: kernels, raising=False)


def test_one_active_kernel_needs_only_the_hidden_url(setup, monkeypatch, capsys):
    discover(monkeypatch, [KERNEL])

    def no_manual_id(_):
        pytest.fail("A single active kernel must be discovered without a manual ID")

    monkeypatch.setattr("builtins.input", no_manual_id)
    configure_kaggle.main()
    saved = kaggle.read_connection(setup)
    assert saved.kernel_id == KERNEL
    assert saved.server_url.get_secret_value() == SERVER
    assert setup.stat().st_mode & 0o777 == 0o600
    assert "synthetic-credential" not in capsys.readouterr().out


def test_failed_discovery_preserves_the_working_connection(setup, monkeypatch):
    setup.parent.mkdir(exist_ok=True)
    setup.write_text("previous connection")

    def expired(_):
        raise LLMError("Kaggle session expired; copy a fresh VSCode Compatible URL.")

    monkeypatch.setattr(configure_kaggle, "discover_kernels", expired, raising=False)
    monkeypatch.setattr("builtins.input", lambda _: str(KERNEL))
    with pytest.raises(LLMError, match="expired"):
        configure_kaggle.main()
    assert setup.read_text() == "previous connection"


def test_multiple_kernels_require_the_loaded_models_id(setup, monkeypatch):
    discover(monkeypatch, [KERNEL, OTHER])
    monkeypatch.setattr("builtins.input", lambda _: str(OTHER))
    configure_kaggle.main()
    assert kaggle.read_connection(setup).kernel_id == OTHER


def test_a_kernel_outside_the_live_session_is_refused(setup, monkeypatch):
    discover(monkeypatch, [KERNEL, OTHER])
    monkeypatch.setattr("builtins.input", lambda _: str(UUID(int=7)))
    with pytest.raises(LLMError, match="active kernel"):
        configure_kaggle.main()
    assert not setup.exists()


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_expired_sessions_explain_the_separate_jobrunner_refresh(
    tmp_path: Path, monkeypatch, status: int
):
    path = tmp_path / "connection.json"
    path.write_text(json.dumps({"server_url": SERVER, "kernel_id": str(KERNEL)}))
    path.chmod(0o600)

    @asynccontextmanager
    async def expired(*args, **kwargs):
        raise InvalidStatus(Response(status, "Expired", Headers({"X-Private": SERVER})))
        yield  # pragma: no cover

    monkeypatch.setattr(kaggle, "SessionConnection", expired)
    with pytest.raises(LLMError, match="session expired") as raised:
        await kaggle.KaggleProvider(connection_file=path).complete("system", "synthetic prompt")
    assert "scripts.configure_kaggle" in str(raised.value)
    assert SERVER not in str(raised.value)
    assert raised.value.__suppress_context__


def transport(monkeypatch, status=200, payload=None, failure=None):
    """No HTTP request leaves these tests, including the credential path."""
    seen = {}

    class HTTPS:
        def __init__(self, host, **kwargs):
            seen.update(host=host, **kwargs)

        def request(self, method, path):
            seen.update(method=method, path=path)
            if failure:
                raise OSError(failure)

        def getresponse(self):
            return SimpleNamespace(
                status=status,
                read=lambda limit: (payload or json.dumps([{"id": str(KERNEL)}]).encode())[:limit],
            )

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(configure_kaggle.http.client, "HTTPSConnection", HTTPS)
    return seen


def test_discovery_uses_only_the_validated_host_without_logging(monkeypatch, caplog):
    from pydantic import SecretStr

    seen = transport(monkeypatch)
    assert configure_kaggle.discover_kernels(SecretStr(SERVER)) == [KERNEL]
    assert seen == {
        "host": "kkb-production.jupyter-proxy.kaggle.net",
        "timeout": 20,
        "method": "GET",
        "path": "/k/123/synthetic-credential/proxy/api/kernels",
        "closed": True,
    }
    assert "synthetic-credential" not in caplog.text


@pytest.mark.parametrize("status", [301, 302, 401, 403, 404, 500])
def test_discovery_fails_closed_without_redirecting_or_overwriting(setup, monkeypatch, status):
    seen = transport(monkeypatch, status=status)
    with pytest.raises(LLMError) as raised:
        configure_kaggle.main()
    assert not setup.exists()
    assert seen["closed"]
    assert "synthetic-credential" not in str(raised.value)


@pytest.mark.parametrize(
    "payload",
    [b"[]", b"not json", b'{"id": "bad"}', b'[{"id":"bad"}]', b"x" * 65537],
    ids=["empty", "not-json", "not-list", "invalid-id", "oversized"],
)
def test_bad_or_empty_discovery_is_not_saved(setup, monkeypatch, payload):
    transport(monkeypatch, payload=payload)
    with pytest.raises(LLMError):
        configure_kaggle.main()
    assert not setup.exists()


def test_network_error_does_not_expose_url(setup, monkeypatch):
    transport(monkeypatch, failure=SERVER)
    with pytest.raises(LLMError) as raised:
        configure_kaggle.main()
    assert SERVER not in str(raised.value)
    assert raised.value.__suppress_context__


def test_a_different_host_is_refused_before_network(setup, monkeypatch):
    seen = transport(monkeypatch)
    monkeypatch.setattr(configure_kaggle.getpass, "getpass", lambda _: "https://example.net/")
    with pytest.raises(LLMError):
        configure_kaggle.main()
    assert not seen and not setup.exists()
