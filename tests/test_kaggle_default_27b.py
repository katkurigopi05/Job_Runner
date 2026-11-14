"""Startup must release 8B before loading 27B; no real kernel or model."""

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from packages.llm import kaggle, kaggle_setup
from packages.llm.kaggle_connection import save_connection
from packages.llm.provider import LLMError
from tests.test_kaggle_setup import SAVED, SERVER, FakeSession

MODEL27 = "unsloth/Qwen3.8-27B-GGUF:UD-Q6_K"


@pytest.fixture(autouse=True)
def synthetic_dataset(monkeypatch):
    from packages.core.config import get_settings

    monkeypatch.setattr(
        get_settings(), "kaggle_qwen27_dataset", "example-dev/qwen38-27b-q6-private/versions/1"
    )


@pytest.fixture
def saved_file(tmp_path):
    path = tmp_path / "connection.json"
    save_connection(kaggle.Connection(server_url=SecretStr(SERVER), kernel_id=SAVED), path)
    return path


def fake_runtime(monkeypatch, session, *, fail=False):
    events = []

    async def probe(connection):
        k = session.state[connection.kernel_id]
        return {name: bool(k.get(name)) for name in ("model", "helper", "qwen38_model")}

    class Provider:
        model = None

        def __init__(self, *, connection):
            self.connection = connection

        async def health(self):
            k = session.state[self.connection.kernel_id]
            model = MODEL27 if k.get("qwen38_model") else kaggle.MODEL
            ready = k.get("helper") and (self.model is None or self.model == model)
            return SimpleNamespace(
                state="ready" if ready else "model_not_ready",
                model=model,
                detail="Ready" if ready else "Not ready",
            )

    async def step(connection, code, name, timeout, say):
        k = session.state[connection.kernel_id]
        if "JOBRUNNER_8B_UNLOADED" in code:
            events.append("unload8")
            k.update(model=False, helper=False)
        elif "load_qwen38.py" in code:
            assert not any(v.get("model") for v in session.state.values())
            events.append("load27")
            if fail:
                raise LLMError("Saved Qwen bundle unavailable")
            k["qwen38_model"] = True
        elif "jobrunner_qwen38_complete = install" in code:
            assert k.get("qwen38_model")
            events.append("helper27")
            k["helper"] = True
        else:
            pytest.fail("Unexpected model or install path")

    monkeypatch.setattr(kaggle_setup, "probe", probe)
    monkeypatch.setattr(kaggle_setup, "KaggleProvider", Provider)
    monkeypatch.setattr(kaggle_setup, "_step", step)
    return events


async def test_default_replaces_ready_8b_after_unloading(saved_file, monkeypatch):
    session = FakeSession({SAVED: {"model": True, "helper": True, "clients": 1}})
    events = fake_runtime(monkeypatch, session)
    state = await kaggle_setup.setup(session, kaggle.read_connection(saved_file), saved_file)
    assert state == "ready"
    assert events == ["unload8", "load27", "helper27"]
    assert session.state[SAVED]["qwen38_model"]
    assert session.deleted == []


async def test_default_reuses_ready_27b_without_loading(saved_file, monkeypatch):
    session = FakeSession({SAVED: {"qwen38_model": True, "helper": True}})
    events = fake_runtime(monkeypatch, session)
    assert (
        await kaggle_setup.setup(session, kaggle.read_connection(saved_file), saved_file) == "ready"
    )
    assert events == []


async def test_new_kernel_loads_27b_and_never_falls_back(saved_file, monkeypatch):
    session = FakeSession({})
    events = fake_runtime(monkeypatch, session, fail=True)
    before = saved_file.read_bytes()
    with pytest.raises(LLMError, match="bundle unavailable"):
        await kaggle_setup.setup(session, kaggle.read_connection(saved_file), saved_file)
    assert events == ["load27"]
    assert saved_file.read_bytes() == before
    assert session.deleted == session.started


async def test_busy_saved_kernel_is_not_replaced(saved_file, monkeypatch):
    session = FakeSession({SAVED: {"busy": True, "model": True}})
    events = fake_runtime(monkeypatch, session)
    with pytest.raises(LLMError, match="running"):
        await kaggle_setup.setup(session, kaggle.read_connection(saved_file), saved_file)
    assert events == []
    assert session.deleted == session.started == []


async def test_fresh_connection_is_saved_only_after_27b_is_ready(saved_file, monkeypatch):
    session = FakeSession({})
    events = fake_runtime(monkeypatch, session)
    assert (
        await kaggle_setup.setup(session, kaggle.read_connection(saved_file), saved_file) == "ready"
    )
    assert events == ["load27", "helper27"]
    assert kaggle.read_connection(saved_file).kernel_id == session.started[0]


async def test_loaded_27b_gets_only_its_missing_helper(saved_file, monkeypatch):
    session = FakeSession({SAVED: {"qwen38_model": True}})
    events = fake_runtime(monkeypatch, session)
    await kaggle_setup.setup(session, kaggle.read_connection(saved_file), saved_file)
    assert events == ["helper27"]


async def test_ready_wrong_model_cannot_complete_setup(saved_file, monkeypatch):
    session = FakeSession({})
    fake_runtime(monkeypatch, session)

    async def wrong_model(self):
        return SimpleNamespace(state="ready", model=kaggle.MODEL, detail="8B ready")

    monkeypatch.setattr(kaggle_setup.KaggleProvider, "health", wrong_model)
    before = saved_file.read_bytes()
    with pytest.raises(LLMError, match="27B did not become ready"):
        await kaggle_setup.setup(session, kaggle.read_connection(saved_file), saved_file)
    assert saved_file.read_bytes() == before
    assert session.deleted == session.started


def test_saved_loader_checksum_is_checked_before_execution(tmp_path, monkeypatch):
    import sys

    from packages.llm.kaggle_qwen27 import load_27b

    calls = []
    (tmp_path / "load_qwen38.py").write_text("raise AssertionError('must not execute')")

    def dataset_download(name):
        calls.append(name)
        return str(tmp_path)

    monkeypatch.setitem(
        sys.modules, "kagglehub", SimpleNamespace(dataset_download=dataset_download)
    )
    with pytest.raises(RuntimeError, match="checksum differs"):
        exec(compile(load_27b("helper"), "saved_loader", "exec"), {})
    assert calls == ["example-dev/qwen38-27b-q6-private/versions/1"]


#: The three settings of the saved loader that the startup cell changes, as it writes them.
SAVED_LOADER = """
bundle = None
engine = dict(n_ctx=4096, n_batch=256)
identity = dict(gpu_count=2, context_tokens=4096, thinking=False)
helper = (bundle / 'qwen38_helper.py').read_text()
"""


def run_startup_cell(tmp_path, monkeypatch, saved_loader):
    import hashlib
    import sys

    from packages.llm.kaggle_qwen27 import load_27b

    (tmp_path / "load_qwen38.py").write_text(saved_loader)
    monkeypatch.setitem(
        sys.modules, "kagglehub", SimpleNamespace(dataset_download=lambda name: str(tmp_path))
    )
    kernel: dict = {}
    cell = load_27b(
        "the checkout's helper",
        loader_sha256=hashlib.sha256(saved_loader.encode()).hexdigest(),
    )
    exec(compile(cell, "saved_loader", "exec"), kernel)
    return kernel


def test_saved_loader_runs_with_the_larger_context_and_this_checkout_helper(tmp_path, monkeypatch):
    """The saved loader says 4,096 and installs its own 4,096 helper."""
    kernel = run_startup_cell(tmp_path, monkeypatch, SAVED_LOADER)

    assert kernel["engine"] == {"n_ctx": 16384, "n_batch": 256}
    assert kernel["identity"]["context_tokens"] == 16384
    assert kernel["helper"] == "the checkout's helper"


@pytest.mark.parametrize(
    "missing",
    ["n_ctx=4096,", "context_tokens=4096,", "(bundle / 'qwen38_helper.py').read_text()"],
)
def test_a_saved_loader_without_an_expected_setting_is_not_run(tmp_path, monkeypatch, missing):
    """Loading at a context nobody chose is worse than not loading."""
    changed = SAVED_LOADER.replace(missing, "None,").replace(
        "bundle = None", "raise AssertionError('must not execute')"
    )

    with pytest.raises(RuntimeError, match="refusing to change it"):
        run_startup_cell(tmp_path, monkeypatch, changed)
