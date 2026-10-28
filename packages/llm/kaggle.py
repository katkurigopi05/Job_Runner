"""Call a loaded Kaggle notebook model through its authenticated Jupyter kernel.

The session URL embeds a credential in its *path*. It is held as a secret,
never logged, never put in an exception, and never sent to another host.
The notebook helper is installed explicitly; no model is loaded automatically.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from websockets.asyncio.client import connect as WebSocketConnect
from websockets.exceptions import InvalidStatus

from packages.llm.audit import record
from packages.llm.provider import DEFAULT_TEMPERATURE, LLMError, PromptTooLong

MODEL = "unsloth/Qwen3-8B-bnb-4bit"
QWEN38_MODEL = "unsloth/Qwen3.8-27B-GGUF:UD-Q6_K"
QWEN38_SHA256 = "c9c206812fbe4ac7b76a729e25928b63f2ae89d37f69da7a71c20aec763cd436"
# Auto chooses the first ready supported model, then pins it for this request.
HELPERS = {QWEN38_MODEL: "jobrunner_qwen38_complete", MODEL: "jobrunner_complete"}
MODEL_LABELS = {QWEN38_MODEL: "Qwen3.8 27B Q6_K", MODEL: "Qwen3 8B 4-bit"}
T = TypeVar("T", bound=BaseModel)

# WebSocket debug logs include the credential-bearing request path. This
# connection gets its own disabled logger, rather than changing global logs.
_TRANSPORT_LOG = logging.Logger("jobrunner.kaggle.transport")
_TRANSPORT_LOG.disabled = True


class SessionConnection(WebSocketConnect):
    """An expired notebook must fail rather than redirect private context."""

    def process_redirect(self, exc: Exception) -> Exception:
        return exc


class Connection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_url: SecretStr
    kernel_id: UUID

    def socket_url(self) -> str:
        url = self.server_url.get_secret_value().rstrip("/")
        if not re.fullmatch(
            r"https://kkb-production\.jupyter-proxy\.kaggle\.net/k/[0-9]+/"
            r"[A-Za-z0-9_.-]+/proxy",
            url,
        ):
            raise LLMError("Use the full Kaggle VSCode Compatible URL for the running session.")
        return url.replace("https://", "wss://", 1) + f"/api/kernels/{self.kernel_id}/channels"


def read_connection(path: Path) -> Connection:
    """Read only an owner-private regular file; refuse symlinks and bad input."""
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise LLMError("Kaggle connection must be an owner-owned regular file.")
            if info.st_mode & 0o077:
                raise LLMError("Kaggle connection file must have permissions 0600.")
            connection = Connection.model_validate_json(stream.read(65537))
        connection.socket_url()
        return connection
    except LLMError:
        raise
    except Exception:
        raise LLMError(
            "Kaggle connection is missing or invalid. Run scripts/configure_kaggle.py "
            "with the running notebook's VSCode Compatible URL and kernel ID."
        ) from None


State = Literal[
    "unconfigured",
    "unreachable",
    "expired",
    "kernel_missing",
    "helper_missing",
    "helper_outdated",
    "model_not_ready",
    "ready",
    "select_kernel",
]

DETAILS: dict[str, str] = {
    "unconfigured": (
        "Kaggle is not connected to Jobrunner. Paste the current VSCode Compatible URL here."
    ),
    "unreachable": (
        "Jobrunner cannot reach the saved Kaggle session. Check the notebook and "
        "reconnect here; IDE connectivity is separate."
    ),
    "expired": (
        "Kaggle session expired or is unavailable. Reconnect here, or run "
        "scripts.configure_kaggle with the current session URL."
    ),
    "kernel_missing": (
        "The saved kernel is no longer active in this Kaggle session. Reconnect here to "
        "select the current kernel, or run `make kaggle-setup`."
    ),
    "helper_missing": (
        "Kaggle kernel is reachable, but the Jobrunner helper is not loaded. Run the "
        "model and helper cell in this kernel, or run `make kaggle-setup`."
    ),
    "helper_outdated": (
        "Kaggle kernel is reachable, but its helper cannot verify readiness. Run the "
        "current Jobrunner helper cell again, or run `make kaggle-setup`."
    ),
    "model_not_ready": (
        "Kaggle is connected, but no supported loaded GPU model is ready for this request. "
        "Run the helper for your loaded model, then check the connection."
    ),
    "ready": "A supported Kaggle GPU model and its Jobrunner helper are ready.",
    "select_kernel": (
        "Several kernels are active. Select the kernel where you loaded the model and helper."
    ),
}


class KaggleStatus(BaseModel):
    """Public connection state, never its credential-bearing server URL."""

    state: State
    detail: str
    kernel_id: UUID | None = None
    kernels: list[UUID] = Field(default_factory=list)
    model: str | None = None
    context_tokens: int | None = None


class ConnectionFailure(LLMError):
    def __init__(self, state: State) -> None:
        self.state = state
        super().__init__(DETAILS[state])


def connection_path() -> Path:
    """CLI and API share one file even when launched from different directories."""
    from packages.core.config import ENV_FILE_VARIABLE, get_settings, settings_env_file

    path = get_settings().kaggle_connection_file.expanduser()
    root = (
        settings_env_file().parent
        if os.environ.get(ENV_FILE_VARIABLE, "").strip()
        else Path(__file__).resolve().parents[2]
    )
    return path if path.is_absolute() else root / path


HEALTH_EXPRESSION = (
    "__import__('json').dumps({'helpers': ["
    "dict(globals()[name].jobrunner_health(), helper=name) "
    "if callable(getattr(globals()[name], 'jobrunner_health', None)) "
    "else {'state': 'helper_outdated', 'helper': name} "
    f"for name in {tuple(HELPERS.values())!r} if callable(globals().get(name))]}})"
)


class KaggleProvider:
    """Explicit remote provider; no prompt is sent until readiness is verified."""

    name = "kaggle"
    model: str | None = None
    REQUEST_TIMEOUT_S = 300.0

    def __init__(
        self, *, connection_file: Path | None = None, connection: Connection | None = None
    ) -> None:
        self.connection = connection or read_connection(connection_file or connection_path())
        self.connection.socket_url()
        self._helper: str | None = None

    async def _exchange(self, expression: str, *, timeout: float) -> dict[str, Any]:
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
                "code": "",
                "silent": True,
                "store_history": False,
                "allow_stdin": False,
                "stop_on_error": True,
                "user_expressions": {"jobrunner": expression},
            },
        }
        try:
            async with asyncio.timeout(timeout):
                async with SessionConnection(
                    self.connection.socket_url(),
                    logger=_TRANSPORT_LOG,
                    open_timeout=20,
                    max_size=1024 * 1024,
                    proxy=None,
                ) as socket:
                    await socket.send(json.dumps(request))
                    while True:
                        message = json.loads(await socket.recv())
                        if message.get("parent_header", {}).get("msg_id") != message_id:
                            continue
                        if (
                            message.get("msg_type", message.get("header", {}).get("msg_type"))
                            != "execute_reply"
                        ):
                            continue
                        item = message["content"]["user_expressions"]["jobrunner"]
                        if item["status"] != "ok":
                            raise LLMError(DETAILS["helper_missing"])
                        result = json.loads(ast.literal_eval(item["data"]["text/plain"]))
                        if not isinstance(result, dict):
                            raise LLMError(DETAILS["helper_outdated"])
                        return result
        except LLMError:
            raise
        except InvalidStatus as exc:
            state: State = (
                "expired" if exc.response.status_code in {401, 403, 404} else "unreachable"
            )
            raise ConnectionFailure(state) from None
        except Exception:
            # Handshake exceptions can contain the private URL and headers.
            raise ConnectionFailure("unreachable") from None

    async def health(self) -> KaggleStatus:
        try:
            result = await self._exchange(HEALTH_EXPRESSION, timeout=20)
        except ConnectionFailure as exc:
            return KaggleStatus(state=exc.state, detail=DETAILS[exc.state])
        except LLMError:
            return KaggleStatus(state="helper_outdated", detail=DETAILS["helper_outdated"])
        # Flat reports remain accepted for the original protocol-1 8B helper.
        reports = result.get("helpers", [result])
        if not isinstance(reports, list) or not all(isinstance(row, dict) for row in reports):
            reports = [{"state": "helper_outdated"}]
        ready = {}
        for row in reports:
            model = row.get("model")
            if (
                row.get("state") == "ready"
                and isinstance(model, str)
                and model in HELPERS
                and row.get("helper", "jobrunner_complete") == HELPERS[model]
                and type(row.get("protocol")) is int
                and row["protocol"] == 1
                and type(row.get("context_tokens")) is int
                and row["context_tokens"] == 4096
                and (
                    model != QWEN38_MODEL
                    or (
                        type(row.get("gpu_count")) is int
                        and row["gpu_count"] == 2
                        and row.get("thinking") is False
                        and row.get("model_sha256") == QWEN38_SHA256
                    )
                )
            ):
                ready[model] = row
        chosen = (
            self.model
            if self.model is not None
            else next((model for model in HELPERS if model in ready), None)
        )
        if chosen is not None and chosen in ready:
            self.model = chosen
            self._helper = HELPERS[chosen]
            return KaggleStatus(
                state="ready",
                detail=f"Auto selected {MODEL_LABELS[chosen]} on Kaggle GPU. The helper is ready.",
                kernel_id=self.connection.kernel_id,
                model=chosen,
                context_tokens=4096,
            )
        states = {row.get("state") for row in reports if isinstance(row.get("state"), str)}
        state: State = (
            "helper_missing"
            if not reports or states == {"helper_missing"}
            else "helper_outdated"
            if states == {"helper_outdated"}
            else "model_not_ready"
        )
        return KaggleStatus(
            state=state,
            detail=DETAILS[state],
            kernel_id=self.connection.kernel_id,
        )

    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        if not 1 <= max_tokens <= 1024 or not 0 <= temperature <= 2:
            raise LLMError("Kaggle answer budget or temperature is outside the supported range.")
        health = await self.health()
        if health.state != "ready":
            raise LLMError(health.detail) from None
        assert self.model is not None and self._helper in HELPERS.values()
        payload = json.dumps(
            dict(
                model=self.model,
                system=system,
                user=user,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        )
        record(self.name, system, user, model=self.model)
        result = await self._exchange(
            f"{self._helper}({payload!r})", timeout=self.REQUEST_TIMEOUT_S
        )
        if result.get("model") != self.model:
            raise LLMError("The Kaggle model changed during this request. Check the connection.")
        if result.get("status") == "too_long":
            raise PromptTooLong("Kaggle's 4096-token context cannot fit the prompt and answer.")
        text = result.get("text")
        if result.get("status") != "ok" or not isinstance(text, str) or not text.strip():
            raise LLMError(
                "The loaded Kaggle model returned no usable answer. Check its helper cell."
            )
        return text.strip()

    async def complete_json(self, system: str, user: str, schema: type[T]) -> T:
        raise LLMError("Kaggle is available for dashboard chat only, not structured tasks.")
