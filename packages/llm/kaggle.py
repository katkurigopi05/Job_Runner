"""Call a loaded Kaggle notebook model through its authenticated Jupyter kernel.

The session URL embeds a credential in its *path*. It is held as a secret,
never logged, never put in an exception, and never sent to another host.
The notebook helper is installed explicitly; no model is loaded automatically.

Kaggle issues a new session URL every time a notebook session starts, so a
saved connection goes stale on every restart, and connecting the GPU or an IDE
inside Kaggle does not update it. A failure therefore says which link failed
(by the time it was saved, never its text) and the one command that replaces
it. A restarted *kernel* inside a live session keeps the link and only changes
the kernel id, so that case is followed here, by the same rule setup uses: one
running kernel is chosen, several are never guessed between.
"""

from __future__ import annotations

import ast
import asyncio
import http.client
import json
import logging
import os
import re
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import structlog
from pydantic import BaseModel, ConfigDict, SecretStr, TypeAdapter
from websockets.asyncio.client import connect as WebSocketConnect
from websockets.exceptions import InvalidStatus

from packages.llm.audit import record
from packages.llm.provider import DEFAULT_TEMPERATURE, LLMError, PromptTooLong

MODEL = "unsloth/Qwen3-8B-bnb-4bit"
SESSION_HOST = "kkb-production.jupyter-proxy.kaggle.net"
#: What the owner runs when the saved link no longer reaches a session.
RECONNECT = "make kaggle-connect"
_UNVERIFIED = "Kaggle session could not be verified; connection not changed."
T = TypeVar("T", bound=BaseModel)

log = structlog.get_logger(__name__)

# WebSocket debug logs include the credential-bearing request path. This
# connection gets its own disabled logger, rather than changing global logs.
_TRANSPORT_LOG = logging.Logger("jobrunner.kaggle.transport")
_TRANSPORT_LOG.disabled = True


class SessionGone(LLMError):
    """The saved link reaches no running Kaggle session: it did not answer."""


class SessionExpired(SessionGone):
    """Kaggle refused the saved link (401, 403 or 404)."""


class _KernelRefused(LLMError):
    """The kernel channel refused the handshake; the session may still be up."""


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


class _Kernel(BaseModel):
    id: UUID


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
            f"Kaggle connection is missing or invalid. Run `{RECONNECT}` with the "
            "running notebook's VSCode Compatible URL."
        ) from None


def save_connection(destination: Path, connection: Connection) -> None:
    """Replace the saved connection atomically, readable by the owner only."""
    connection.socket_url()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkstemp creates the file 0600, before anything is written to it.
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".kaggle-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(
                json.dumps(
                    {
                        "server_url": connection.server_url.get_secret_value(),
                        "kernel_id": str(connection.kernel_id),
                    }
                )
            )
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def saved_when(path: Path) -> str:
    """When the connection was saved, in words; the file's text is never read."""
    try:
        saved = datetime.fromtimestamp(os.lstat(path).st_mtime)
    except OSError:
        return "earlier"
    return f"on {saved:%b} {saved.day} at {saved:%H:%M}"


def list_kernels(server_url: SecretStr) -> list[UUID]:
    """The session's running kernels, which may be none.

    Never follows a credential redirect. http.client has no URL logger or
    environment proxy handling. TLS uses its default certificate checks, and
    the host and path are validated before any I/O. It lists kernels only: it
    runs no notebook code and calls no model.
    """
    Connection(server_url=server_url, kernel_id=UUID(int=0)).socket_url()
    path = urlsplit(server_url.get_secret_value().rstrip("/")).path
    client = http.client.HTTPSConnection(SESSION_HOST, timeout=20)
    try:
        try:
            client.request("GET", path + "/api/kernels")
            response = client.getresponse()
        except Exception:
            raise SessionGone("Kaggle session did not answer.") from None
        if response.status in {401, 403, 404}:
            raise SessionExpired("Kaggle session expired.")
        if response.status != 200:
            raise LLMError(_UNVERIFIED)
        payload = response.read(65537)
        if len(payload) > 65536:
            raise LLMError(_UNVERIFIED)
        try:
            kernels = TypeAdapter(list[_Kernel]).validate_json(payload)
        except Exception:
            raise LLMError(_UNVERIFIED) from None
        return [kernel.id for kernel in kernels]
    finally:
        client.close()


class KaggleProvider:
    """Dashboard-only remote provider; no automatic routing or local fallback."""

    name = "kaggle"
    model = MODEL
    REQUEST_TIMEOUT_S = 300.0

    def __init__(self, *, connection_file: Path | None = None) -> None:
        from packages.core.config import get_settings

        self.connection_file = connection_file or get_settings().kaggle_connection_file
        self.connection = read_connection(self.connection_file)

    def _ended(self) -> str:
        return (
            f"The Kaggle session link Jobrunner saved {saved_when(self.connection_file)} "
            "is not answering. Kaggle gives every session a new link, and connecting "
            "the GPU or an IDE in Kaggle does not update Jobrunner. If the notebook was "
            f"stopped or restarted, run `{RECONNECT}` in a terminal and paste the "
            "current session's VSCode Compatible URL."
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
        payload = json.dumps(
            dict(
                model=self.model,
                system=system,
                user=user,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        )
        # Recorded once: a refused handshake delivers nothing, so a retry on a
        # followed kernel is the first and only time the prompt leaves.
        record(self.name, system, user, model=self.model)
        try:
            result = await self._exchange(payload)
        except _KernelRefused:
            await self._follow_restarted_kernel()
            try:
                result = await self._exchange(payload)
            except _KernelRefused:
                raise LLMError(self._ended()) from None
        if not isinstance(result, dict) or result.get("model") != self.model:
            raise LLMError("The Kaggle notebook does not have the expected Qwen3 8B model.")
        if result.get("status") == "too_long":
            raise PromptTooLong("Kaggle's 4096-token context cannot fit the prompt and answer.")
        text = result.get("text")
        if result.get("status") != "ok" or not isinstance(text, str) or not text.strip():
            raise LLMError(
                "The loaded Kaggle model returned no usable answer. Check its helper cell."
            )
        return text.strip()

    def _request(self, payload: str) -> tuple[str, dict[str, Any]]:
        message_id = uuid4().hex
        return message_id, {
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
                # repr makes the entire JSON a string literal. Posting text
                # and questions can never escape into Python source.
                "user_expressions": {"jobrunner": f"jobrunner_complete({payload!r})"},
            },
        }

    async def _exchange(self, payload: str) -> object:
        """One request over the kernel channel, each failure named for what it was."""
        message_id, request = self._request(payload)
        opened = False
        try:
            async with asyncio.timeout(self.REQUEST_TIMEOUT_S):
                async with SessionConnection(
                    self.connection.socket_url(),
                    logger=_TRANSPORT_LOG,
                    open_timeout=20,
                    max_size=1024 * 1024,
                    proxy=None,
                ) as socket:
                    opened = True
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
                        expression = message["content"]["user_expressions"]["jobrunner"]
                        if expression["status"] != "ok":
                            raise LLMError(
                                "The Kaggle kernel has no Jobrunner helper: it was restarted "
                                "or never set up. Load the model, run the helper cell "
                                "(`jobrunner_complete = install(model, tokenizer)`), then ask "
                                "again."
                            )
                        return json.loads(ast.literal_eval(expression["data"]["text/plain"]))
        except LLMError:
            raise
        except InvalidStatus as exc:
            if exc.response.status_code in {401, 403, 404}:
                raise _KernelRefused(self._ended()) from None
            raise LLMError("Kaggle session did not answer. Check the running notebook.") from None
        except Exception:
            # Exceptions from the handshake can quote both URL and response.
            # Neither may reach an API error or traceback via a chained cause.
            if opened:
                raise LLMError(
                    "The Kaggle session connected, but the kernel did not finish answering. "
                    "Check that no other cell is running in the notebook, then ask again."
                ) from None
            raise SessionGone(self._ended()) from None

    async def _follow_restarted_kernel(self) -> None:
        """Adopt the one kernel a live session runs now, or say why not."""
        try:
            kernels = await asyncio.to_thread(list_kernels, self.connection.server_url)
        except Exception:
            raise SessionGone(self._ended()) from None
        if self.connection.kernel_id in kernels:
            raise LLMError(
                "The Kaggle session is running and still lists the saved kernel, but the "
                "kernel refused the connection. Check the notebook, then ask again."
            )
        if not kernels:
            raise LLMError(
                "The Kaggle session is running but has no kernel. Load the model, run the "
                "Jobrunner helper cell, then ask again."
            )
        if len(kernels) > 1:
            raise LLMError(
                f"The notebook's kernel was restarted, and the session now runs "
                f"{len(kernels)} kernels. Run `{RECONNECT}` and enter the ID of the one "
                "holding the loaded model."
            )
        followed = Connection(server_url=self.connection.server_url, kernel_id=kernels[0])
        save_connection(self.connection_file, followed)
        self.connection = followed
        # No ids: the kernel id is part of the credential.
        log.info("kaggle_kernel_followed")

    async def complete_json(self, system: str, user: str, schema: type[T]) -> T:
        raise LLMError("Kaggle is available for dashboard chat only, not structured tasks.")
