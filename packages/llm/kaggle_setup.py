"""Prepare the saved Kaggle session for Jobrunner: one kernel, the model, the helper.

`make kaggle-setup`. After a new Kaggle session the only manual step is saving
its link (Reconnect in the assistant, or `scripts.configure_kaggle`). This does
the rest, reusing what is already there before loading anything:

1. A saved kernel that already reports ready is left alone.
2. A kernel that already holds the 4-bit model gets the helper installed.
3. Otherwise a new kernel is started; the libraries are installed, the model is
   found among the session's attached inputs by its config (no path is written
   in this repository), copied to working disk and loaded on the GPU; and the
   helper is sent from this checkout's `scripts/kaggle_helper.py` over the
   authenticated kernel channel, so there is no download and no hash to pin.

Jobrunner is then pointed at that kernel, and kernels nobody uses (idle, no
client attached) are deleted, which frees the GPU memory a second copy of the
model would hold. `fresh=True` deletes every kernel first. A kernel that is
running a cell, or that has a client such as VS Code attached, is never
deleted unless `fresh` asks for it. Nothing here prints the session link or a
kernel id.
"""

from __future__ import annotations

import ast
import asyncio
import http.client
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import SecretStr

from packages.llm import kaggle
from packages.llm.kaggle import DETAILS, Connection, ConnectionFailure, KaggleProvider
from packages.llm.kaggle_connection import save_connection
from packages.llm.provider import LLMError

SESSION_HOST = "kkb-production.jupyter-proxy.kaggle.net"
HELPER_FILE = Path(__file__).resolve().parents[2] / "scripts" / "kaggle_helper.py"
QWEN38_HELPER_FILE = HELPER_FILE.with_name("kaggle_qwen38_helper.py")

INSTALL = (
    '%pip install -q -U "transformers>=4.51.0" accelerate bitsandbytes '
    'huggingface_hub safetensors "pillow>=8.0,<13.0"'
)

#: Runs in the kernel. Pure standard library, so it can be tested here.
FIND_MODEL = """
def jobrunner_find_model(root="/kaggle/input"):
    import json
    from pathlib import Path
    found = []
    for config in sorted(Path(root).rglob("config.json")):
        try:
            data = json.loads(config.read_text())
        except Exception:
            continue
        quant = data.get("quantization_config") or {}
        if (
            data.get("model_type") == "qwen3"
            and data.get("hidden_size") == 4096
            and data.get("num_hidden_layers") == 36
            and (quant.get("load_in_4bit") or quant.get("quant_method") == "bitsandbytes")
        ):
            found.append(config.parent)
    if len(found) != 1:
        raise RuntimeError(
            f"Expected one attached 4-bit Qwen3 8B model under {root}, found {len(found)}. "
            "Attach the model dataset to the notebook."
        )
    return found[0]
"""

#: Runs in the kernel. The copy to working disk and the synchronous load are
#: what resolved the stalled load recorded in docs/KAGGLE_ASSISTANT.md.
LOAD_MODEL = """
import os, shutil, time
os.environ["HF_DEACTIVATE_ASYNC_LOAD"] = "1"
from pathlib import Path
import torch
from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer
MODEL_DIR = Path("/kaggle/working/models/qwen3-8b-4bit")
if not (MODEL_DIR / ".jobrunner-copied").exists():
    started = time.perf_counter()
    shutil.copytree(jobrunner_find_model(), MODEL_DIR, dirs_exist_ok=True)
    (MODEL_DIR / ".jobrunner-copied").touch()
    seconds = round(time.perf_counter() - started)
    print("Copied the model to working disk in", seconds, "s", flush=True)
config = AutoConfig.from_pretrained(MODEL_DIR, local_files_only=True)
config.quantization_config["bnb_4bit_compute_dtype"] = "float16"
tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR, local_files_only=True)
started = time.perf_counter()
model = AutoModelForCausalLM.from_pretrained(
    MODEL_DIR, config=config, dtype=torch.float16, device_map={"": 0},
    local_files_only=True, disable_mmap=True,
).eval()
torch.cuda.synchronize()
assert model.is_loaded_in_4bit and model.device.type == "cuda"
print("Loaded the model on the GPU in", round(time.perf_counter() - started), "s", flush=True)
"""

#: A user expression: never runs a cell, reads two facts about the kernel.
PROBE = (
    "__import__('json').dumps({"
    "'model': bool(getattr(globals().get('model'), 'is_loaded_in_4bit', False)) "
    "and getattr(getattr(globals().get('model'), 'device', None), 'type', None) == 'cuda', "
    "'helper': any(callable(getattr(globals().get(name), 'jobrunner_health', None)) "
    f"for name in {tuple(kaggle.HELPERS.values())!r}), "
    "'qwen38_model': all(name in globals() for name in "
    "('jobrunner27_engine', 'jobrunner27_tokenizer', 'jobrunner27_identity'))"
    "})"
)

_IDS = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def helper_cell(*, qwen38: bool = False) -> str:
    """The helper from this checkout, as a literal: nothing is downloaded."""
    source = (QWEN38_HELPER_FILE if qwen38 else HELPER_FILE).read_text(encoding="utf-8")
    install = (
        "jobrunner_qwen38_complete = install(jobrunner27_engine, jobrunner27_tokenizer, "
        "jobrunner27_identity)\n"
        if qwen38
        else "jobrunner_complete = install(model, tokenizer)\n"
    )
    return (
        f"exec(compile({source!r}, 'jobrunner_kaggle_helper', 'exec'))\n"
        f"{install}"
        "print('Installed the Jobrunner helper.', flush=True)\n"
    )


def scrub(line: str) -> str:
    """Kernel output may echo paths or ids; neither the link nor an id is printed."""
    return _IDS.sub("<id>", re.sub(r"https?://\S+", "<url>", line))


@dataclass(frozen=True)
class KernelInfo:
    label: int
    id: UUID
    busy: bool
    clients: int


class Session:
    """The saved session's Jupyter server, reached on the validated host only."""

    def __init__(self, server_url: SecretStr) -> None:
        Connection(server_url=server_url, kernel_id=UUID(int=0)).socket_url()
        self.server_url = server_url
        self._base = urlsplit(server_url.get_secret_value().rstrip("/")).path + "/api/kernels"

    def _call(self, method: str, suffix: str = "", body: dict[str, Any] | None = None) -> Any:
        client = http.client.HTTPSConnection(SESSION_HOST, timeout=30)
        try:
            headers = {"Content-Type": "application/json"} if body is not None else {}
            payload = json.dumps(body) if body is not None else None
            client.request(method, self._base + suffix, body=payload, headers=headers)
            response = client.getresponse()
            data = response.read(1024 * 1024)
        except Exception:
            # The request line carries the credential; never chain or quote it.
            raise ConnectionFailure("unreachable") from None
        finally:
            client.close()
        if response.status in {401, 403, 404} and method == "GET":
            raise ConnectionFailure("expired")
        if response.status == 404 and method == "DELETE":
            return None  # already gone
        if response.status >= 400:
            raise LLMError(f"Kaggle refused the request (HTTP {response.status}).")
        return json.loads(data) if data else None

    def kernels(self) -> list[KernelInfo]:
        rows = self._call("GET") or []
        return [
            KernelInfo(
                label=n,
                id=UUID(row["id"]),
                busy=row.get("execution_state") != "idle",
                clients=int(row.get("connections") or 0),
            )
            for n, row in enumerate(rows, 1)
        ]

    def start(self) -> UUID:
        return UUID(self._call("POST", body={"name": "python3"})["id"])

    def delete(self, kernel_id: UUID) -> None:
        self._call("DELETE", f"/{kernel_id}")

    def connection(self, kernel_id: UUID) -> Connection:
        return Connection(server_url=self.server_url, kernel_id=kernel_id)


async def _send(
    connection: Connection, code: str, expressions: dict[str, str], timeout: float
) -> tuple[bool, list[str], dict[str, Any]]:
    """One execute_request; returns success, printed lines and expression results."""
    message_id = uuid4().hex
    request = {
        "header": {
            "msg_id": message_id,
            "username": "jobrunner",
            "session": uuid4().hex,
            "msg_type": "execute_request",
            "version": "5.3",
            "date": datetime.now(UTC).isoformat(),
        },
        "parent_header": {},
        "metadata": {},
        "channel": "shell",
        "buffers": [],
        "content": {
            "code": code,
            "silent": not code,
            "store_history": False,
            "allow_stdin": False,
            "stop_on_error": True,
            "user_expressions": expressions,
        },
    }
    lines: list[str] = []
    ok = True
    try:
        async with asyncio.timeout(timeout):
            async with kaggle.SessionConnection(
                connection.socket_url(),
                logger=kaggle._TRANSPORT_LOG,
                open_timeout=30,
                max_size=8 * 1024 * 1024,
                proxy=None,
            ) as socket:
                await socket.send(json.dumps(request))
                while True:
                    message = json.loads(await socket.recv())
                    if message.get("parent_header", {}).get("msg_id") != message_id:
                        continue
                    kind = message.get("msg_type") or message.get("header", {}).get("msg_type")
                    content = message.get("content", {})
                    if kind == "stream":
                        lines += [scrub(line) for line in content.get("text", "").splitlines()]
                    elif kind == "error":
                        ok = False
                        lines.append(scrub(f"{content.get('ename')}: {content.get('evalue')}"))
                    elif kind == "execute_reply":
                        ok = ok and content.get("status") == "ok"
                        return ok, lines, content.get("user_expressions", {})
    except TimeoutError:
        return False, [*lines, f"no reply within {round(timeout)} s"], {}
    except Exception:
        # Handshake errors can quote the credential-bearing URL.
        raise ConnectionFailure("unreachable") from None


async def probe(connection: Connection) -> dict[str, bool]:
    _, _, results = await _send(connection, "", {"jobrunner": PROBE}, timeout=30)
    item = results.get("jobrunner", {})
    if item.get("status") != "ok":
        return {"model": False, "helper": False}
    facts = json.loads(ast.literal_eval(item["data"]["text/plain"]))
    return {key: bool(facts.get(key)) for key in ("model", "helper", "qwen38_model")}


Say = Callable[[str], None]


async def _step(connection: Connection, code: str, name: str, timeout: float, say: Say) -> None:
    say(f"{name}...")
    ok, lines, _ = await _send(connection, code, {}, timeout)
    if not ok:
        detail = "; ".join(line for line in lines[-4:] if line.strip()) or "no output"
        raise LLMError(f"{name} failed: {detail[:400]}")
    for line in lines:
        if line.startswith(("Copied", "Loaded", "Installed")):
            say(f"  {line}")


async def setup(
    session: Session,
    saved: Connection,
    destination: Path,
    *,
    fresh: bool = False,
    say: Say = print,
) -> str:
    """Make one kernel ready, point Jobrunner at it, delete unused ones; returns the state."""
    kernels = session.kernels()
    say(f"The session runs {len(kernels)} kernel(s).")
    if fresh:
        for kernel in kernels:
            session.delete(kernel.id)
        say(f"Deleted {len(kernels)} kernel(s) to start fresh.")
        kernels = []

    chosen: UUID | None = None
    facts: dict[UUID, dict[str, bool]] = {}
    for kernel in kernels:
        if kernel.busy:
            say(f"Kernel {kernel.label} is running a cell; left alone.")
            continue
        facts[kernel.id] = await probe(session.connection(kernel.id))
    ready = [k for k in kernels if facts.get(k.id, {}).get("helper")]
    loaded = [
        k
        for k in kernels
        if facts.get(k.id, {}).get("model") or facts.get(k.id, {}).get("qwen38_model")
    ]
    if ready:
        pick = next((k for k in ready if k.id == saved.kernel_id), ready[0])
        chosen = pick.id
        say(f"Kernel {pick.label} already has the model and the helper.")
    elif loaded:
        pick = next((k for k in loaded if k.id == saved.kernel_id), loaded[0])
        chosen = pick.id
        say(f"Kernel {pick.label} already has the model; installing the helper.")
        await _step(
            session.connection(chosen),
            helper_cell(qwen38=facts[chosen].get("qwen38_model", False)),
            "Installing the helper",
            180,
            say,
        )
    else:
        chosen = session.start()
        say("Started a new kernel.")
        connection = session.connection(chosen)
        try:
            await _step(connection, INSTALL, "Installing libraries (about 1-2 min)", 900, say)
            await _step(
                connection,
                FIND_MODEL + LOAD_MODEL,
                "Loading the model (about 3 min)",
                1500,
                say,
            )
            await _step(connection, helper_cell(), "Installing the helper", 180, say)
        except BaseException:
            # A half-set-up kernel may hold GPU memory; the saved link is unchanged.
            try:
                session.delete(chosen)
                say("Deleted the new kernel; the saved connection is unchanged.")
            except Exception:
                say("Could not delete the new kernel; `make kaggle-setup fresh=1` will.")
            raise

    connection = session.connection(chosen)
    if chosen != saved.kernel_id:
        save_connection(connection, destination)
        say("Jobrunner now uses that kernel.")
    status = await KaggleProvider(connection=connection).health()

    unused = [k for k in kernels if k.id != chosen and not k.busy and k.clients == 0]
    for kernel in unused:
        session.delete(kernel.id)
    if unused:
        say(f"Deleted {len(unused)} unused kernel(s).")
    kept = [k for k in kernels if k.id != chosen and k not in unused]
    if kept:
        say(
            f"Kept {len(kept)} other kernel(s) that are busy or have a client attached; "
            "`make kaggle-setup fresh=1` deletes them too."
        )
    say(status.detail)
    return status.state


def describe_failure(error: LLMError) -> str:
    if isinstance(error, ConnectionFailure):
        return DETAILS[error.state]
    return str(error)
