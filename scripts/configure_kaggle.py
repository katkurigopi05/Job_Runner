"""Save a session connection privately, without editing .env or printing its URL."""

from __future__ import annotations

import getpass
from uuid import UUID

from pydantic import SecretStr

from packages.llm.kaggle import Connection, connection_path
from packages.llm.kaggle_connection import discover_kernels, save_connection
from packages.llm.provider import LLMError


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
    save_connection(connection, connection_path())
    print(
        "Kaggle connection saved privately. "
        "Check Kaggle readiness in the assistant before asking a model."
    )


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
