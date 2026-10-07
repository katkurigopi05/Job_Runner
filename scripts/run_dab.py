"""Start Data API Builder over the public job tables. `make dab`.

Microsoft's Data API Builder turns `apps/dab/dab-config.json` into REST and
GraphQL endpoints: `/api/Posting?$filter=...&$select=...&$first=...`, a
Swagger page at `/swagger`, and `/graphql`. Three sources, read only; what it
may serve is held by `tests/test_data_api_builder.py`.

This launcher exists because three things about starting it are not safe to
leave to whoever types the command:

- **Where it listens.** This machine only, and that is not a setting. The
  port can move (`JOBRUNNER_DAB_PORT`): 5000, the tool's default, is macOS's
  AirPlay receiver.
- **Where it starts.** The tool loads `.env` from its working directory. The
  project's holds every key the project has, and the tool cannot parse it
  anyway, so it is started in `apps/dab`, which has none.
- **What it connects as.** The connection string is built here from the
  project's own `DATABASE_URL` and passed in the environment, never written
  to the config or the command line. It asks Postgres to refuse writes in
  every transaction, so the read-only rule does not rest on the config alone.

Install the tool once with `dotnet tool install --global Microsoft.DataApiBuilder`.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

from sqlalchemy.engine import make_url

from packages.core.config import get_settings

CONFIG_DIR = Path(__file__).resolve().parents[1] / "apps" / "dab"
CONFIG_FILE = "dab-config.json"

#: The variable `dab-config.json` reads its connection string from.
CONN_ENV = "JOBRUNNER_DAB_CONN"
PORT_ENV = "JOBRUNNER_DAB_PORT"

HOST = "127.0.0.1"
DEFAULT_PORT = 5050

#: Sent to Postgres with the connection: every transaction starts read-only.
READ_ONLY = "-c default_transaction_read_only=on"

_NEEDS_QUOTING = set(" ;=\"'")


def _value(text: str) -> str:
    """`text` as a connection-string value: quoted when it could end early."""
    if not text or _NEEDS_QUOTING & set(text):
        return '"' + text.replace('"', '""') + '"'
    return text


def connection_string(database_url: str) -> str:
    """The project's SQLAlchemy URL as the `Key=value;` form Npgsql reads."""
    url = make_url(database_url)
    parts = {
        "Host": url.host or "localhost",
        "Port": str(url.port or 5432),
        "Database": url.database or "",
        "Username": url.username or "",
        "Password": url.password or "",
        "Options": READ_ONLY,
    }
    return "".join(f"{key}={_value(value)};" for key, value in parts.items())


def _port(env: Mapping[str, str]) -> int:
    raw = env.get(PORT_ENV)
    if raw is None:
        return DEFAULT_PORT
    if not raw.isdecimal() or not 1 <= int(raw) <= 65535:
        sys.exit(f"{PORT_ENV} must be a port number between 1 and 65535, not {raw!r}")
    return int(raw)


def environment(database_url: str, base: Mapping[str, str]) -> dict[str, str]:
    """What the tool is started with: `base`, where to listen, what to connect as."""
    return {
        **base,
        CONN_ENV: connection_string(database_url),
        "ASPNETCORE_URLS": f"http://{HOST}:{_port(base)}",
        "DAB_TELEMETRY_OPTOUT": "1",
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    }


def main() -> None:
    tool = shutil.which("dab")
    if tool is None:
        sys.exit(
            "Data API Builder is not installed. Install it once with:\n"
            "  dotnet tool install --global Microsoft.DataApiBuilder"
        )
    env = environment(get_settings().database_url, os.environ)
    os.chdir(CONFIG_DIR)
    os.execve(tool, [tool, "start", "--config", CONFIG_FILE], env)


if __name__ == "__main__":
    main()
