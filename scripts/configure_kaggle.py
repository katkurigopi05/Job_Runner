"""Save a session connection privately, without editing .env or printing its URL.

Kaggle issues a new link for every notebook session, so this runs once per
session. `make kaggle-connect` runs it and then `scripts.check_kaggle`, which
sends one short test prompt; this script itself runs no notebook code.
"""

from __future__ import annotations

import getpass
from uuid import UUID

from pydantic import SecretStr

from packages.core.config import get_settings
from packages.llm.kaggle import (
    Connection,
    SessionExpired,
    SessionGone,
    list_kernels,
    save_connection,
)
from packages.llm.provider import LLMError


def discover_kernels(server_url: SecretStr) -> list[UUID]:
    """Check the fresh session before saving; a failure changes nothing.

    Discovery lists kernels only: it runs no notebook code or model calls.
    """
    try:
        kernels = list_kernels(server_url)
    except SessionExpired:
        raise LLMError("Kaggle session expired; copy a fresh VSCode Compatible URL.") from None
    except SessionGone:
        raise LLMError("Kaggle session could not be verified; connection not changed.") from None
    except LLMError:
        raise
    except Exception:
        raise LLMError("Kaggle session could not be verified; connection not changed.") from None
    if not kernels:
        raise LLMError("No active Kaggle kernel. Run a notebook cell, then reconnect.")
    return kernels


def main() -> None:
    server_url = SecretStr(getpass.getpass("Kaggle VSCode Compatible URL (hidden): ").strip())
    kernels = discover_kernels(server_url)
    kernel_id = (
        kernels[0] if len(kernels) == 1 else UUID(input("Loaded model's kernel ID: ").strip())
    )
    if kernel_id not in kernels:
        raise LLMError("Choose an active kernel from this Kaggle session.")
    save_connection(
        get_settings().kaggle_connection_file,
        Connection(server_url=server_url, kernel_id=kernel_id),
    )
    print("Kaggle connection saved privately.")


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
