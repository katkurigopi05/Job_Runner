"""Running pg_dump and pg_restore on this machine.

The owner's Postgres runs in Docker and the host has no client tools, while CI
has host tools and a service container. So the binaries come from the host
when present and from `docker exec` into the container otherwise.

The password never goes on a command line, where any local process listing
could read it: host tools get `PGPASSWORD` in their environment, and
`docker exec -e PGPASSWORD` forwards the variable by name from that same
environment.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.engine import make_url

DEFAULT_CONTAINER = "jobrunner-postgres"


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    user: str
    database: str
    password: str = field(default="", repr=False)


def target_from_url(url: str, *, database: str | None = None) -> Target:
    parsed = make_url(url)
    return Target(
        host=parsed.host or "localhost",
        port=parsed.port or 5432,
        user=parsed.username or "postgres",
        database=database or parsed.database or "postgres",
        password=parsed.password or "",
    )


@dataclass(frozen=True)
class PgTools:
    mode: Literal["host", "docker"]
    container: str | None = None

    def command(self, tool: str, target: Target, args: list[str]) -> list[str]:
        if self.mode == "host":
            return [tool, "-h", target.host, "-p", str(target.port), "-U", target.user, *args]
        # Inside the container the server is on its own socket, whatever port
        # the host maps it to.
        assert self.container is not None
        return [
            "docker",
            "exec",
            "-i",
            "-e",
            "PGPASSWORD",
            self.container,
            tool,
            "-U",
            target.user,
            *args,
        ]

    def env(self, target: Target) -> dict[str, str]:
        return {**os.environ, "PGPASSWORD": target.password}

    def describe(self) -> str:
        return "host pg_dump/pg_restore" if self.mode == "host" else f"docker exec {self.container}"


def resolve_tools(container: str | None = None) -> PgTools | None:
    """Host tools if installed, else the running Postgres container, else None."""
    if shutil.which("pg_dump") and shutil.which("pg_restore"):
        return PgTools("host")
    name = container or os.environ.get("BACKUP_PG_CONTAINER") or DEFAULT_CONTAINER
    if shutil.which("docker"):
        probe = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if probe.returncode == 0 and probe.stdout.strip() == "true":
            return PgTools("docker", name)
    return None


__all__ = ["DEFAULT_CONTAINER", "PgTools", "Target", "resolve_tools", "target_from_url"]
