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
from typing import TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, SecretStr
from websockets.asyncio.client import connect as WebSocketConnect

from packages.llm.audit import record
from packages.llm.provider import DEFAULT_TEMPERATURE, LLMError, PromptTooLong

MODEL = "unsloth/Qwen3-8B-bnb-4bit"
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


class KaggleProvider:
    """Dashboard-only remote provider; no automatic routing or local fallback."""

    name = "kaggle"
    model = MODEL
    REQUEST_TIMEOUT_S = 300.0

    def __init__(self, *, connection_file: Path | None = None) -> None:
        from packages.core.config import get_settings

        self.connection = read_connection(connection_file or get_settings().kaggle_connection_file)

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
                # repr makes the entire JSON a string literal. Posting text
                # and questions can never escape into Python source.
                "user_expressions": {"jobrunner": f"jobrunner_complete({payload!r})"},
            },
        }
        record(self.name, system, user, model=self.model)
        try:
            async with asyncio.timeout(self.REQUEST_TIMEOUT_S):
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
                        expression = message["content"]["user_expressions"]["jobrunner"]
                        if expression["status"] != "ok":
                            raise LLMError(
                                "Run the Jobrunner helper cell in the loaded Kaggle notebook."
                            )
                        result = json.loads(ast.literal_eval(expression["data"]["text/plain"]))
                        break
        except LLMError:
            raise
        except Exception:
            # Exceptions from the handshake can quote both URL and response.
            # Neither may reach an API error or traceback via a chained cause.
            raise LLMError(
                "Kaggle session did not answer. Check that the notebook is running, "
                "then refresh its connection URL and kernel ID."
            ) from None
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

    async def complete_json(self, system: str, user: str, schema: type[T]) -> T:
        raise LLMError("Kaggle is available for dashboard chat only, not structured tasks.")
