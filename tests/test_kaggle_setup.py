"""`make kaggle-setup` reuses what is loaded, never guesses, and never prints the link.

A fake session stands in for Kaggle's Jupyter server: kernels that may hold the
model or the helper, run cells, and answer the same user expressions the real
code sends. No test opens a network connection.
"""

from __future__ import annotations

import ast
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from packages.llm import kaggle, kaggle_setup
from packages.llm.kaggle import Connection, ConnectionFailure
from packages.llm.kaggle_connection import save_connection
from packages.llm.provider import LLMError

SECRET = "synthetic-credential"
SERVER = f"https://kkb-production.jupyter-proxy.kaggle.net/k/123/{SECRET}/proxy"
SAVED = UUID("00000000-0000-4000-8000-000000000001")
OTHER = UUID("00000000-0000-4000-8000-000000000002")


class FakeSession:
    """Kernels by id: {"model", "helper", "busy", "clients"}; records what was done."""

    def __init__(self, kernels: dict[UUID, dict], *, fail_load: bool = False) -> None:
        self.state = kernels
        self.fail_load = fail_load
        self.started: list[UUID] = []
        self.deleted: list[UUID] = []
        self.cells: list[tuple[UUID, str]] = []
        self.server_url = SecretStr(SERVER)

    def kernels(self) -> list[kaggle_setup.KernelInfo]:
        return [
            kaggle_setup.KernelInfo(n, kid, k.get("busy", False), k.get("clients", 0))
            for n, (kid, k) in enumerate(self.state.items(), 1)
        ]

    def start(self) -> UUID:
        kid = uuid4()
        self.state[kid] = {}
        self.started.append(kid)
        return kid

    def delete(self, kernel_id: UUID) -> None:
        self.deleted.append(kernel_id)
        self.state.pop(kernel_id, None)

    def connection(self, kernel_id: UUID) -> Connection:
        return Connection(server_url=self.server_url, kernel_id=kernel_id)


def serve(monkeypatch, session: FakeSession) -> None:
    """Patch the kernel channel: run cells and user expressions against `session`."""

    class Socket:
        def __init__(self, kernel_id: UUID) -> None:
            self.kernel_id = kernel_id
            self.queue: list[dict] = []

        async def send(self, value: str) -> None:
            request = json.loads(value)
            parent = {"msg_id": request["header"]["msg_id"]}
            content = request["content"]
            kernel = session.state[self.kernel_id]
            code = content["code"]
            if code:
                session.cells.append((self.kernel_id, code))
            out, error = [], None
            if code.startswith("%pip"):
                out.append("Successfully installed transformers")
            elif "jobrunner_find_model" in code:
                if session.fail_load:
                    error = ("RuntimeError", f"Expected one attached model at {SERVER}")
                else:
                    kernel["model"] = True
                    out += [
                        "Copied the model to working disk in 140 s",
                        "Loaded the model on the GPU in 20 s",
                    ]
            elif "install(model, tokenizer)" in code:
                if kernel.get("model"):
                    kernel["helper"] = True
                    out.append("Installed the Jobrunner helper.")
                else:
                    error = ("NameError", "name 'model' is not defined")
            elif "install(jobrunner27_engine" in code:
                kernel["qwen38_helper"] = True
                out.append("Installed the Jobrunner helper.")
            results = {}
            for name, expression in content["user_expressions"].items():
                if expression == kaggle_setup.PROBE:
                    value = {
                        "model": bool(kernel.get("model")),
                        "helper": bool(kernel.get("helper") or kernel.get("qwen38_helper")),
                        "qwen38_model": bool(kernel.get("qwen38_model")),
                    }
                elif "jobrunner_health" in expression:
                    value = (
                        {
                            "state": "ready",
                            "protocol": 1,
                            "model": kaggle.QWEN38_MODEL
                            if kernel.get("qwen38_helper")
                            else kaggle.MODEL,
                            "context_tokens": 4096,
                            **(
                                {
                                    "helper": "jobrunner_qwen38_complete",
                                    "gpu_count": 2,
                                    "thinking": False,
                                    "model_sha256": kaggle.QWEN38_SHA256,
                                }
                                if kernel.get("qwen38_helper")
                                else {}
                            ),
                        }
                        if kernel.get("helper") or kernel.get("qwen38_helper")
                        else {"state": "helper_missing"}
                    )
                else:
                    value = None
                results[name] = {"status": "ok", "data": {"text/plain": repr(json.dumps(value))}}
            for line in out:
                self.queue.append(
                    {
                        "msg_type": "stream",
                        "parent_header": parent,
                        "content": {"text": line + "\n"},
                    }
                )
            if error:
                self.queue.append(
                    {
                        "msg_type": "error",
                        "parent_header": parent,
                        "content": {"ename": error[0], "evalue": error[1]},
                    }
                )
            self.queue.append(
                {
                    "msg_type": "execute_reply",
                    "parent_header": parent,
                    "content": {"status": "error" if error else "ok", "user_expressions": results},
                }
            )

        async def recv(self) -> str:
            return json.dumps(self.queue.pop(0))

    @asynccontextmanager
    async def connect(url, **kwargs):
        assert kwargs["logger"].disabled and kwargs["proxy"] is None
        yield Socket(UUID(url.rsplit("/kernels/", 1)[1].split("/")[0]))

    monkeypatch.setattr(kaggle, "SessionConnection", connect)


@pytest.fixture
def saved_file(tmp_path: Path) -> Path:
    path = tmp_path / "storage" / "kaggle-connection.json"
    save_connection(Connection(server_url=SecretStr(SERVER), kernel_id=SAVED), path)
    return path


async def run(session: FakeSession, path: Path, *, fresh: bool = False) -> tuple[str, str]:
    said: list[str] = []
    saved = kaggle.read_connection(path)
    state = await kaggle_setup.setup(session, saved, path, fresh=fresh, say=said.append)
    return state, "\n".join(said)


def assert_private(output: str, session: FakeSession) -> None:
    assert SECRET not in output
    for kid in [SAVED, OTHER, *session.started]:
        assert str(kid) not in output


async def test_a_ready_saved_kernel_is_left_alone(saved_file, monkeypatch):
    session = FakeSession({SAVED: {"model": True, "helper": True, "clients": 1}})
    serve(monkeypatch, session)
    before = saved_file.read_bytes()
    state, said = await run(session, saved_file)
    assert state == "ready"
    assert session.started == [] and session.deleted == [] and session.cells == []
    assert saved_file.read_bytes() == before
    assert_private(said, session)


async def test_auto_keeps_ready_27b_without_loading_8b(saved_file, monkeypatch):
    session = FakeSession({SAVED: {"qwen38_model": True, "qwen38_helper": True, "clients": 1}})
    serve(monkeypatch, session)
    state, said = await run(session, saved_file)
    assert state == "ready"
    assert session.started == session.deleted == session.cells == []
    assert "Qwen3.8 27B" in said
    assert_private(said, session)


async def test_loaded_27b_gets_only_its_helper(saved_file, monkeypatch):
    session = FakeSession({SAVED: {"qwen38_model": True}})
    serve(monkeypatch, session)
    state, said = await run(session, saved_file)
    assert state == "ready" and session.started == []
    assert [code for _, code in session.cells] == [kaggle_setup.helper_cell(qwen38=True)]
    assert "Loading the model" not in said
    assert_private(said, session)


async def test_a_kernel_holding_the_model_gets_only_the_helper(saved_file, monkeypatch):
    session = FakeSession({SAVED: {"model": True}})
    serve(monkeypatch, session)
    state, said = await run(session, saved_file)
    assert state == "ready"
    assert session.started == []
    assert [code for _, code in session.cells] == [kaggle_setup.helper_cell()]
    assert "installing the helper" in said


async def test_otherwise_a_new_kernel_is_set_up_saved_and_the_unused_one_deleted(
    saved_file, monkeypatch
):
    session = FakeSession({SAVED: {}, OTHER: {"clients": 2}})
    serve(monkeypatch, session)
    state, said = await run(session, saved_file)

    assert state == "ready"
    [fresh] = session.started
    steps = [code for kid, code in session.cells if kid == fresh]
    assert steps[0] == kaggle_setup.INSTALL
    assert "jobrunner_find_model()" in steps[1]
    assert steps[2] == kaggle_setup.helper_cell()
    saved = kaggle.read_connection(saved_file)
    assert saved.kernel_id == fresh and saved.server_url.get_secret_value() == SERVER
    assert saved_file.stat().st_mode & 0o777 == 0o600
    # The idle, unattached kernel is gone; the one VS Code holds is kept.
    assert session.deleted == [SAVED] and OTHER in session.state
    assert "Kept 1 other kernel" in said and "fresh=1" in said
    assert "Loaded the model on the GPU" in said
    assert_private(said, session)


async def test_fresh_deletes_every_kernel_first(saved_file, monkeypatch):
    session = FakeSession(
        {SAVED: {"model": True, "helper": True, "clients": 2}, OTHER: {"busy": True}}
    )
    serve(monkeypatch, session)
    state, said = await run(session, saved_file, fresh=True)
    assert state == "ready"
    assert session.deleted[:2] == [SAVED, OTHER]
    assert list(session.state) == session.started
    assert "Deleted 2 kernel(s) to start fresh" in said


async def test_a_busy_kernel_is_neither_probed_nor_deleted(saved_file, monkeypatch):
    session = FakeSession({SAVED: {"busy": True}})
    serve(monkeypatch, session)
    state, said = await run(session, saved_file)
    assert state == "ready"
    assert SAVED in session.state and SAVED not in session.deleted
    assert all(kid != SAVED for kid, _ in session.cells)
    assert "running a cell; left alone" in said


async def test_a_failed_load_deletes_the_new_kernel_and_keeps_the_saved_link(
    saved_file, monkeypatch
):
    session = FakeSession({SAVED: {}}, fail_load=True)
    serve(monkeypatch, session)
    before = saved_file.read_bytes()
    said: list[str] = []
    with pytest.raises(LLMError, match="Loading the model") as raised:
        await kaggle_setup.setup(
            session, kaggle.read_connection(saved_file), saved_file, say=said.append
        )
    [fresh] = session.started
    assert fresh in session.deleted
    assert saved_file.read_bytes() == before
    assert SECRET not in str(raised.value)
    assert "saved connection is unchanged" in "\n".join(said)


def test_the_helper_is_sent_from_this_checkout_not_downloaded():
    cell = kaggle_setup.helper_cell()
    source = (Path(__file__).resolve().parents[1] / "scripts" / "kaggle_helper.py").read_text()
    assert repr(source) in cell
    assert "urlopen" not in cell
    assert "jobrunner_complete = install(model, tokenizer)" in cell


def qwen3(path: Path, **overrides) -> None:
    path.mkdir(parents=True)
    config = {
        "model_type": "qwen3",
        "hidden_size": 4096,
        "num_hidden_layers": 36,
        "quantization_config": {"load_in_4bit": True, "quant_method": "bitsandbytes"},
        **overrides,
    }
    (path / "config.json").write_text(json.dumps(config))


def test_the_model_is_found_by_its_config_not_a_written_path(tmp_path):
    namespace: dict = {}
    exec(compile(kaggle_setup.FIND_MODEL, "find", "exec"), namespace)  # noqa: S102
    find = namespace["jobrunner_find_model"]

    qwen3(tmp_path / "datasets" / "owner" / "qwen")
    qwen3(tmp_path / "datasets" / "other" / "llama", model_type="llama")
    qwen3(tmp_path / "datasets" / "other" / "full-precision", quantization_config={})
    assert find(str(tmp_path)) == tmp_path / "datasets" / "owner" / "qwen"

    qwen3(tmp_path / "datasets" / "second" / "qwen")
    with pytest.raises(RuntimeError, match="found 2"):
        find(str(tmp_path))
    with pytest.raises(RuntimeError, match="found 0"):
        find(str(tmp_path / "empty-nothing-here"))


def test_kernel_output_is_scrubbed():
    line = f"loaded from {SERVER} into {SAVED}"
    assert kaggle_setup.scrub(line) == "loaded from <url> into <id>"


def http(monkeypatch, status: int = 200, payload: bytes = b"[]", failure: str | None = None):
    seen: list[tuple] = []

    class HTTPS:
        def __init__(self, host, **kwargs):
            seen.append(("host", host))

        def request(self, method, path, body=None, headers=None):
            seen.append((method, path))
            if failure:
                raise OSError(failure)

        def getresponse(self):
            return SimpleNamespace(status=status, read=lambda limit: payload)

        def close(self):
            seen.append(("closed",))

    monkeypatch.setattr(kaggle_setup.http.client, "HTTPSConnection", HTTPS)
    return seen


def test_the_session_api_uses_the_validated_host_and_path(monkeypatch):
    payload = json.dumps([{"id": str(SAVED), "execution_state": "idle", "connections": 2}]).encode()
    seen = http(monkeypatch, payload=payload)
    session = kaggle_setup.Session(SecretStr(SERVER))
    [kernel] = session.kernels()
    assert (kernel.id, kernel.busy, kernel.clients) == (SAVED, False, 2)
    assert ("host", "kkb-production.jupyter-proxy.kaggle.net") in seen
    assert ("GET", f"/k/123/{SECRET}/proxy/api/kernels") in seen
    assert ("closed",) in seen


def test_an_expired_session_and_a_gone_kernel(monkeypatch):
    session = kaggle_setup.Session(SecretStr(SERVER))
    http(monkeypatch, status=403)
    with pytest.raises(ConnectionFailure) as raised:
        session.kernels()
    assert raised.value.state == "expired"
    http(monkeypatch, status=404, payload=b"")
    session.delete(SAVED)  # already gone is fine


def test_a_network_error_never_quotes_the_link(monkeypatch):
    http(monkeypatch, failure=f"cannot reach {SERVER}")
    with pytest.raises(ConnectionFailure) as raised:
        kaggle_setup.Session(SecretStr(SERVER)).kernels()
    assert SECRET not in str(raised.value)
    assert raised.value.__suppress_context__


def test_a_different_host_is_refused_before_any_request(monkeypatch):
    seen = http(monkeypatch)
    with pytest.raises(LLMError):
        kaggle_setup.Session(SecretStr("https://example.net/k/1/x/proxy"))
    assert seen == []


def test_the_command_without_a_saved_session_says_what_to_do(tmp_path, monkeypatch, capsys):
    from scripts import kaggle_setup as command

    monkeypatch.setattr(command, "connection_path", lambda: tmp_path / "absent.json")
    monkeypatch.setattr("sys.argv", ["kaggle_setup"])
    assert command.main() == 1
    assert "Reconnect in the assistant" in capsys.readouterr().out


def test_probe_reads_only_two_facts():
    expression = kaggle_setup.PROBE
    assert ast.parse(expression, mode="eval")
    assert "jobrunner_complete(" not in expression  # never runs the model


class CommandFakes:
    """Stand-ins for the command's session, setup and terminal."""

    def __init__(self, monkeypatch, tmp_path: Path, *, saved: bool, reachable: bool, tty: bool):
        from scripts import kaggle_setup as command

        self.command = command
        self.listed: list[str] = []
        self.setup_calls: list[Connection] = []
        self.prompted = 0
        path = tmp_path / "kaggle-connection.json"
        if saved:
            save_connection(Connection(server_url=SecretStr(SERVER), kernel_id=SAVED), path)
        fakes = self

        class Session:
            def __init__(self, server_url):
                kaggle_setup.Session(server_url)  # the real host check
                self.server_url = server_url

            def kernels(self):
                fakes.listed.append(self.server_url.get_secret_value())
                if self.server_url.get_secret_value() == SERVER and not reachable:
                    raise ConnectionFailure("expired")
                return []

        async def setup(session, connection, destination, *, fresh=False):
            fakes.setup_calls.append(connection)
            return "ready"

        def ask(prompt):
            fakes.prompted += 1
            return NEW_SERVER

        monkeypatch.setattr(command, "Session", Session)
        monkeypatch.setattr(command, "setup", setup)
        monkeypatch.setattr(command, "connection_path", lambda: path)
        monkeypatch.setattr(command.getpass, "getpass", ask)
        monkeypatch.setattr(command.sys.stdin, "isatty", lambda: tty)
        monkeypatch.setattr("sys.argv", ["kaggle_setup"])


NEW_SERVER = "https://kkb-production.jupyter-proxy.kaggle.net/k/456/new-synthetic/proxy"


def test_a_working_saved_link_is_used_without_asking(tmp_path, monkeypatch):
    fakes = CommandFakes(monkeypatch, tmp_path, saved=True, reachable=True, tty=True)
    assert fakes.command.main() == 0
    assert fakes.prompted == 0
    assert fakes.setup_calls[0].kernel_id == SAVED


@pytest.mark.parametrize("saved", [False, True], ids=["none-saved", "saved-but-expired"])
def test_without_a_working_link_it_asks_in_a_hidden_prompt(tmp_path, monkeypatch, capsys, saved):
    fakes = CommandFakes(monkeypatch, tmp_path, saved=saved, reachable=False, tty=True)
    assert fakes.command.main() == 0
    assert fakes.prompted == 1
    # The new link is checked before setup, and no kernel is chosen yet.
    assert fakes.listed[-1] == NEW_SERVER
    [connection] = fakes.setup_calls
    assert connection.server_url.get_secret_value() == NEW_SERVER
    assert connection.kernel_id == UUID(int=0)
    out = capsys.readouterr().out
    assert "new-synthetic" not in out and SECRET not in out


def test_without_a_terminal_it_explains_instead_of_prompting(tmp_path, monkeypatch, capsys):
    fakes = CommandFakes(monkeypatch, tmp_path, saved=False, reachable=False, tty=False)
    assert fakes.command.main() == 1
    assert fakes.prompted == 0 and fakes.setup_calls == []
    assert "your own terminal" in capsys.readouterr().out


def test_a_pasted_link_on_another_host_is_refused(tmp_path, monkeypatch, capsys):
    fakes = CommandFakes(monkeypatch, tmp_path, saved=False, reachable=False, tty=True)
    monkeypatch.setattr(fakes.command.getpass, "getpass", lambda prompt: "https://example.net/x")
    assert fakes.command.main() == 1
    assert fakes.setup_calls == []
    assert "example.net" not in capsys.readouterr().out


async def test_an_outdated_helper_is_reinstalled_without_reloading_model(saved_file, monkeypatch):
    from unittest.mock import AsyncMock

    session = FakeSession({SAVED: {"model": True, "helper": True}})
    serve(monkeypatch, session)
    monkeypatch.setattr(
        kaggle.KaggleProvider,
        "health",
        AsyncMock(
            side_effect=[
                kaggle.KaggleStatus(state="helper_outdated", detail="Update helper"),
                kaggle.KaggleStatus(state="ready", detail="Ready"),
            ]
        ),
    )
    state, _ = await run(session, saved_file)
    assert state == "ready"
    assert session.started == []
    assert [code for _, code in session.cells] == [kaggle_setup.helper_cell()]
