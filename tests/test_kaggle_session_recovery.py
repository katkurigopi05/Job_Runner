"""A Kaggle failure names the saved link's age and the fix; a restarted kernel is followed.

Kaggle issues a new link per notebook session, so a saved connection goes
stale on every restart. Before this, a dead link waited out the 20-second
handshake and said only "did not answer", which read the same whether the
session had ended, the kernel had restarted, or the helper was missing.
No test here opens a network connection.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import SecretStr
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from packages.llm import kaggle
from packages.llm.audit import read_trail
from packages.llm.provider import LLMError

SECRET = "synthetic-credential"
SERVER = f"https://kkb-production.jupyter-proxy.kaggle.net/k/123/{SECRET}/proxy"
OLD = UUID("00000000-0000-4000-8000-000000000001")
NEW = UUID("00000000-0000-4000-8000-000000000002")
THIRD = UUID("00000000-0000-4000-8000-000000000003")


@pytest.fixture
def saved(tmp_path: Path) -> Path:
    path = tmp_path / "storage" / "kaggle-connection.json"
    kaggle.save_connection(path, kaggle.Connection(server_url=SecretStr(SERVER), kernel_id=OLD))
    return path


class Kernel:
    def __init__(self, *, helper: bool = True, drops: bool = False) -> None:
        self.helper = helper
        self.drops = drops
        self.sent: dict = {}

    async def send(self, value: str) -> None:
        self.sent = json.loads(value)

    async def recv(self) -> str:
        if self.drops:
            raise OSError(f"connection lost {SERVER}")
        result = {"model": kaggle.MODEL, "text": "ready", "status": "ok"}
        expression = (
            {"status": "ok", "data": {"text/plain": repr(json.dumps(result))}}
            if self.helper
            else {"status": "error", "ename": "NameError"}
        )
        return json.dumps(
            {
                "msg_type": "execute_reply",
                "parent_header": {"msg_id": self.sent["header"]["msg_id"]},
                "content": {"status": "ok", "user_expressions": {"jobrunner": expression}},
            }
        )


def channels(monkeypatch, *, serving: UUID | None, kernel: Kernel | None = None) -> list[str]:
    """Only the `serving` kernel accepts; any other id is refused like Jupyter does."""
    opened: list[str] = []

    @asynccontextmanager
    async def connect(url, **kwargs):
        opened.append(url)
        if serving is None or str(serving) not in url:
            raise InvalidStatus(Response(404, "Not Found", Headers({"X-Private": SERVER})))
        yield kernel or Kernel()

    monkeypatch.setattr(kaggle, "SessionConnection", connect)
    return opened


def listed(monkeypatch, kernels: list[UUID]) -> list[int]:
    calls: list[int] = []

    def list_kernels(server_url):
        calls.append(1)
        return kernels

    monkeypatch.setattr(kaggle, "list_kernels", list_kernels)
    return calls


async def test_a_restarted_kernel_is_followed_saved_and_asked_once(saved, monkeypatch):
    opened = channels(monkeypatch, serving=NEW)
    listed(monkeypatch, [NEW])
    trail_before = len(read_trail())

    provider = kaggle.KaggleProvider(connection_file=saved)
    assert await provider.complete("system", "user") == "ready"

    assert len(opened) == 2 and str(NEW) in opened[1]
    followed = kaggle.read_connection(saved)
    assert followed.kernel_id == NEW
    assert followed.server_url.get_secret_value() == SERVER
    assert saved.stat().st_mode & 0o777 == 0o600
    # The refused handshake delivered nothing, so the prompt left once.
    assert len(read_trail()) == trail_before + 1


@pytest.mark.parametrize(
    "kernels,reason",
    [
        ([NEW, THIRD], "now runs 2 kernels"),
        ([OLD], "still lists the saved kernel"),
        ([], "has no kernel"),
    ],
    ids=["several", "same-kernel", "none"],
)
async def test_a_kernel_is_never_guessed_and_the_link_is_kept(saved, monkeypatch, kernels, reason):
    channels(monkeypatch, serving=None)
    listed(monkeypatch, kernels)
    before = saved.read_bytes()
    with pytest.raises(LLMError, match=reason) as raised:
        await kaggle.KaggleProvider(connection_file=saved).complete("system", "user")
    assert saved.read_bytes() == before
    assert SECRET not in str(raised.value)


async def test_a_dead_link_says_when_it_was_saved_and_what_to_run(saved, monkeypatch):
    @asynccontextmanager
    async def never_opens(url, **kwargs):
        raise TimeoutError(f"timed out opening {url}")
        yield  # pragma: no cover

    monkeypatch.setattr(kaggle, "SessionConnection", never_opens)
    calls = listed(monkeypatch, [OLD])
    stamp = datetime(2026, 10, 25, 23, 45).timestamp()
    os.utime(saved, (stamp, stamp))

    with pytest.raises(kaggle.SessionGone) as raised:
        await kaggle.KaggleProvider(connection_file=saved).complete("system", "user")
    message = str(raised.value)
    assert "saved on Oct 25 at 23:45" in message
    assert kaggle.RECONNECT in message
    assert "does not update Jobrunner" in message
    assert SECRET not in message and str(OLD) not in message
    assert raised.value.__suppress_context__
    # Nothing opened, so there is nothing a listing could add.
    assert calls == []


async def test_a_followed_kernel_without_the_helper_says_to_load_and_install(saved, monkeypatch):
    channels(monkeypatch, serving=NEW, kernel=Kernel(helper=False))
    listed(monkeypatch, [NEW])
    with pytest.raises(LLMError, match="no Jobrunner helper") as raised:
        await kaggle.KaggleProvider(connection_file=saved).complete("system", "user")
    assert "install(model, tokenizer)" in str(raised.value)


async def test_a_reply_cut_off_after_connecting_is_not_called_a_dead_session(saved, monkeypatch):
    channels(monkeypatch, serving=OLD, kernel=Kernel(drops=True))
    with pytest.raises(LLMError, match="did not finish answering") as raised:
        await kaggle.KaggleProvider(connection_file=saved).complete("system", "private prompt")
    assert not isinstance(raised.value, kaggle.SessionGone)
    assert SECRET not in str(raised.value) and "private prompt" not in str(raised.value)


class Answers:
    async def complete(self, system, user, **kwargs):
        return "ready"


class Refuses:
    def __init__(self, error: LLMError) -> None:
        self.error = error

    async def complete(self, system, user, **kwargs):
        raise self.error


def checker(monkeypatch, saved: Path, provider):
    from scripts import check_kaggle

    monkeypatch.setattr(
        check_kaggle, "get_settings", lambda: SimpleNamespace(kaggle_connection_file=saved)
    )

    def build(name):
        assert name == "kaggle"
        if isinstance(provider, LLMError):
            raise provider
        return provider

    monkeypatch.setattr(check_kaggle, "build_provider", build)
    return check_kaggle


async def test_check_says_connected_without_printing_the_link(saved, monkeypatch, capsys):
    check_kaggle = checker(monkeypatch, saved, Answers())
    assert await check_kaggle.check() == 0
    out = capsys.readouterr().out
    assert "Connected: Qwen3 8B answered" in out
    assert SECRET not in out and str(OLD) not in out


async def test_check_fails_with_the_providers_reason(saved, monkeypatch, capsys):
    reason = kaggle.SessionGone(f"Saved link is not answering. Run `{kaggle.RECONNECT}`.")
    check_kaggle = checker(monkeypatch, saved, Refuses(reason))
    assert await check_kaggle.check() == 1
    out = capsys.readouterr().out
    assert "Not working" in out and kaggle.RECONNECT in out


async def test_check_without_a_saved_connection(tmp_path, monkeypatch, capsys):
    missing = LLMError(f"Kaggle connection is missing or invalid. Run `{kaggle.RECONNECT}`.")
    check_kaggle = checker(monkeypatch, tmp_path / "absent.json", missing)
    assert await check_kaggle.check() == 1
    assert "Not connected" in capsys.readouterr().out
