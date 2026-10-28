"""One private session store shared by dashboard, CLI and provider."""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, SecretStr, TypeAdapter

from packages.llm.kaggle import (
    DETAILS,
    MODEL_LABELS,
    Connection,
    ConnectionFailure,
    KaggleKernelOption,
    KaggleProvider,
    KaggleStatus,
    connection_path,
    read_connection,
)
from packages.llm.provider import LLMError


class Kernel(BaseModel):
    id: UUID


def discover_kernels(server_url: SecretStr) -> list[UUID]:
    """List active kernels on the exact validated host; never follow redirects."""
    validated = Connection(server_url=server_url, kernel_id=UUID(int=0))
    validated.socket_url()
    url = urlsplit(server_url.get_secret_value().rstrip("/"))
    client = http.client.HTTPSConnection("kkb-production.jupyter-proxy.kaggle.net", timeout=20)
    try:
        client.request("GET", url.path + "/api/kernels")
        response = client.getresponse()
        if response.status in {401, 403, 404}:
            raise ConnectionFailure("expired")
        if response.status != 200:
            raise ConnectionFailure("unreachable")
        payload = response.read(65537)
        if len(payload) > 65536:
            raise ValueError("Kernel response too large")
        kernels = TypeAdapter(list[Kernel]).validate_json(payload)
        if not kernels:
            raise ConnectionFailure("kernel_missing")
        return [kernel.id for kernel in kernels]
    except LLMError:
        raise
    except Exception:
        raise ConnectionFailure("unreachable") from None
    finally:
        client.close()


def save_connection(connection: Connection, destination: Path) -> None:
    """Atomic owner-private replacement; a failed check never reaches this write."""
    connection.socket_url()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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


def failure_status(error: LLMError) -> KaggleStatus:
    state = error.state if isinstance(error, ConnectionFailure) else "unreachable"
    return KaggleStatus(state=state, detail=DETAILS[state])


async def saved_status() -> KaggleStatus:
    try:
        connection = read_connection(connection_path())
    except LLMError:
        return KaggleStatus(state="unconfigured", detail=DETAILS["unconfigured"])
    try:
        kernels = await asyncio.to_thread(discover_kernels, connection.server_url)
    except LLMError as exc:
        return failure_status(exc)
    if connection.kernel_id not in kernels:
        return KaggleStatus(
            state="kernel_missing", detail=DETAILS["kernel_missing"], kernels=kernels
        )
    health = await KaggleProvider(connection=connection).health()
    return health.model_copy(update={"kernels": kernels})


async def connect_session(server_url: SecretStr, kernel_id: UUID | None = None) -> KaggleStatus:
    Connection(server_url=server_url, kernel_id=UUID(int=0)).socket_url()
    try:
        kernels = await asyncio.to_thread(discover_kernels, server_url)
    except LLMError as exc:
        return failure_status(exc)
    options: list[KaggleKernelOption] = []
    if kernel_id is None:
        if len(kernels) != 1:
            statuses = await asyncio.gather(
                *(
                    KaggleProvider(
                        connection=Connection(server_url=server_url, kernel_id=kid)
                    ).health()
                    for kid in kernels
                )
            )
            for index, (kid, status) in enumerate(zip(kernels, statuses, strict=True), 1):
                name = MODEL_LABELS.get(status.model or "", "model not ready")
                if status.state == "helper_missing":
                    name = "no model helper loaded"
                elif status.state != "ready":
                    name = "model or helper unavailable"
                options.append(
                    KaggleKernelOption(
                        id=kid,
                        label=f"Notebook {index} — {name}",
                        state=status.state,
                        model=status.model,
                    )
                )
            ready = [option for option in options if option.state == "ready"]
            if len(ready) != 1:
                return KaggleStatus(
                    state="select_kernel",
                    detail=DETAILS["select_kernel"],
                    kernels=kernels,
                    kernel_options=options,
                )
            kernel_id = ready[0].id
        else:
            kernel_id = kernels[0]
    if kernel_id not in kernels:
        return KaggleStatus(
            state="kernel_missing", detail=DETAILS["kernel_missing"], kernels=kernels
        )
    connection = Connection(server_url=server_url, kernel_id=kernel_id)
    health = await KaggleProvider(connection=connection).health()
    if health.state in {"ready", "helper_missing", "helper_outdated", "model_not_ready"}:
        try:
            save_connection(connection, connection_path())
        except OSError:
            raise LLMError(
                "Kaggle connection could not be saved privately; existing connection preserved."
            ) from None
        health = health.model_copy(update={"detail": "Connection saved. " + health.detail})
    return health.model_copy(update={"kernels": kernels, "kernel_options": options})
