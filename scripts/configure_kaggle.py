"""Save a session connection privately, without editing .env or printing its URL."""

from __future__ import annotations

import getpass
import http.client
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, SecretStr, TypeAdapter

from packages.core.config import get_settings
from packages.llm.kaggle import Connection
from packages.llm.provider import LLMError


class Kernel(BaseModel):
    id: UUID


def discover_kernels(server_url: SecretStr) -> list[UUID]:
    """Check the fresh session before saving; never follow a credential redirect.

    http.client has no URL logger or environment proxy handling. TLS uses its
    default certificate checks, and the host/path are validated before I/O.
    Discovery lists kernels only: it runs no notebook code or model calls.
    """
    validated = Connection(server_url=server_url, kernel_id=UUID(int=0))
    validated.socket_url()
    url = urlsplit(server_url.get_secret_value().rstrip("/"))
    client = http.client.HTTPSConnection("kkb-production.jupyter-proxy.kaggle.net", timeout=20)
    try:
        client.request("GET", url.path + "/api/kernels")
        response = client.getresponse()
        if response.status in {401, 403, 404}:
            raise LLMError("Kaggle session expired; copy a fresh VSCode Compatible URL.")
        if response.status != 200:
            raise LLMError("Kaggle session could not be verified; connection not changed.")
        payload = response.read(65537)
        if len(payload) > 65536:
            raise ValueError("Kernel response is too large")
        kernels = TypeAdapter(list[Kernel]).validate_json(payload)
        if not kernels:
            raise LLMError("No active Kaggle kernel. Run a notebook cell, then reconnect.")
        return [kernel.id for kernel in kernels]
    except LLMError:
        raise
    except Exception:
        raise LLMError("Kaggle session could not be verified; connection not changed.") from None
    finally:
        client.close()


def main() -> None:
    server_url = SecretStr(getpass.getpass("Kaggle VSCode Compatible URL (hidden): ").strip())
    kernels = discover_kernels(server_url)
    kernel_id = (
        kernels[0] if len(kernels) == 1 else UUID(input("Loaded model's kernel ID: ").strip())
    )
    if kernel_id not in kernels:
        raise LLMError("Choose an active kernel from this Kaggle session.")
    connection = Connection(
        server_url=server_url,
        kernel_id=kernel_id,
    )
    connection.socket_url()
    destination = get_settings().kaggle_connection_file
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
    print("Kaggle connection saved privately. Select Kaggle GPU (remote) in the assistant.")


if __name__ == "__main__":
    try:
        main()
    except LLMError as exc:
        print(str(exc))
        raise SystemExit(1) from None
    except (Exception, KeyboardInterrupt):
        # Validation errors include their input. The URL is a credential.
        print("Connection not saved. Check the session URL, kernel ID and private destination.")
        raise SystemExit(1) from None
