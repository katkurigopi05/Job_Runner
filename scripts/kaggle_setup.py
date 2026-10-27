"""`make kaggle-setup`: load the model and helper into the Kaggle session.

Uses the saved session link. When there is none, or it no longer answers (Kaggle
issues a new link for every session), it asks for the current VSCode Compatible
URL in a hidden prompt, which needs a real terminal. Nothing is saved unless
setup succeeds. `make kaggle-setup fresh=1` deletes every kernel first. What it
reuses, loads and deletes is in `packages/llm/kaggle_setup.py`. Never prints
the session link or a kernel id.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from uuid import UUID

from pydantic import SecretStr

from packages.llm.kaggle import Connection, ConnectionFailure, connection_path, read_connection
from packages.llm.kaggle_setup import Session, describe_failure, setup
from packages.llm.provider import LLMError

NO_TERMINAL = (
    "Jobrunner has no working Kaggle session link. Run `make kaggle-setup` in your own "
    "terminal to paste it (hidden), or use Reconnect in the assistant."
)


def _saved_and_reachable() -> Connection | None:
    try:
        saved = read_connection(connection_path())
        Session(saved.server_url).kernels()
    except ConnectionFailure as exc:
        print(describe_failure(exc))
        return None
    except LLMError:
        return None
    return saved


def _ask_for_link() -> Connection | None:
    if not sys.stdin.isatty():
        print(NO_TERMINAL)
        return None
    url = SecretStr(getpass.getpass("Current Kaggle VSCode Compatible URL (hidden): ").strip())
    Session(url).kernels()  # verifies the session answers before anything else
    # No kernel is chosen yet: setup picks or starts one and only then saves.
    return Connection(server_url=url, kernel_id=UUID(int=0))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--fresh", action="store_true", help="delete every kernel in the session first"
    )
    args = parser.parse_args()
    try:
        saved = _saved_and_reachable() or _ask_for_link()
        if saved is None:
            return 1
        state = asyncio.run(
            setup(Session(saved.server_url), saved, connection_path(), fresh=args.fresh)
        )
    except LLMError as exc:
        print(describe_failure(exc))
        return 1
    except (Exception, KeyboardInterrupt) as exc:
        # Exception text from validation or transports can carry the link.
        print(f"Kaggle setup stopped: {type(exc).__name__}.")
        return 1
    return 0 if state == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
