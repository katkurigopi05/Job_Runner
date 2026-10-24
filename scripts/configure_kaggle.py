"""Save a session connection privately, without editing .env or printing its URL."""

from __future__ import annotations

import getpass
import json
import os
import tempfile
from pathlib import Path

from packages.core.config import get_settings
from packages.llm.kaggle import Connection


def main() -> None:
    connection = Connection(
        server_url=getpass.getpass("Kaggle VSCode Compatible URL (hidden): "),
        kernel_id=input("Loaded model's kernel ID: ").strip(),
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
    except (Exception, KeyboardInterrupt):
        # Validation errors include their input. The URL is a credential.
        print("Connection not saved. Check the session URL, kernel ID and private destination.")
        raise SystemExit(1) from None
