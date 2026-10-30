"""Only the verified working notebook is offered when it is already known."""

from uuid import UUID

import pytest
from pydantic import SecretStr

from packages.llm import kaggle
from packages.llm import kaggle_connection as manager

SERVER = SecretStr("https://kkb-production.jupyter-proxy.kaggle.net/k/123/test-secret/proxy")
CURRENT = UUID(int=1)
OTHER = UUID(int=2)
MODEL = "unsloth/Qwen3.8-27B-GGUF:UD-Q6_K"


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "connection.json"
    monkeypatch.setattr(manager, "connection_path", lambda: path)
    monkeypatch.setattr(manager, "discover_kernels", lambda _: [OTHER, CURRENT])
    return path


def serve(monkeypatch, ready):
    seen = []

    async def health(self):
        kid = self.connection.kernel_id
        seen.append(kid)
        return kaggle.KaggleStatus(
            state="ready" if kid in ready else "helper_missing",
            detail="Ready" if kid in ready else "No helper loaded",
            kernel_id=kid,
            model=MODEL if kid in ready else None,
            context_tokens=4096 if kid in ready else None,
        )

    monkeypatch.setattr(kaggle.KaggleProvider, "health", health)
    return seen


async def test_saved_status_shows_only_current_ready_notebook(store, monkeypatch):
    manager.save_connection(kaggle.Connection(server_url=SERVER, kernel_id=CURRENT), store)
    seen = serve(monkeypatch, {CURRENT})
    status = await manager.saved_status()
    assert status.kernels == [CURRENT]
    assert len(status.kernel_options) == 1
    assert status.kernel_options[0].id == CURRENT
    assert "Qwen3.8 27B" in status.kernel_options[0].label
    assert "Current notebook" in status.kernel_options[0].label
    assert seen == [CURRENT]
    assert "test-secret" not in status.model_dump_json()


async def test_same_session_reconnect_keeps_current_without_picker(store, monkeypatch):
    manager.save_connection(kaggle.Connection(server_url=SERVER, kernel_id=CURRENT), store)
    seen = serve(monkeypatch, {CURRENT, OTHER})
    status = await manager.connect_session(SERVER)
    assert status.state == "ready" and status.kernel_id == CURRENT
    assert status.kernels == [CURRENT]
    assert seen == [CURRENT]


async def test_one_ready_notebook_auto_connects_and_hides_empty_kernel(store, monkeypatch):
    serve(monkeypatch, {CURRENT})
    status = await manager.connect_session(SERVER)
    assert status.state == "ready" and status.kernel_id == CURRENT
    assert status.kernels == [CURRENT]
    assert [option.id for option in status.kernel_options] == [CURRENT]
    assert kaggle.read_connection(store).kernel_id == CURRENT


async def test_two_ready_notebooks_without_saved_choice_still_require_selection(store, monkeypatch):
    serve(monkeypatch, {CURRENT, OTHER})
    status = await manager.connect_session(SERVER)
    assert status.state == "select_kernel"
    assert set(status.kernels) == {CURRENT, OTHER}
    assert all("Qwen3.8 27B" in option.label for option in status.kernel_options)
    assert not store.exists()


async def test_different_url_does_not_reuse_saved_notebook_choice(store, monkeypatch):
    old = SERVER.get_secret_value().replace("test-secret", "previous-secret")
    manager.save_connection(kaggle.Connection(server_url=SecretStr(old), kernel_id=CURRENT), store)
    previous = store.read_bytes()
    serve(monkeypatch, {CURRENT, OTHER})
    status = await manager.connect_session(SERVER)
    assert status.state == "select_kernel"
    assert store.read_bytes() == previous
