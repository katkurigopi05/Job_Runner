"""The database is published on this machine's loopback only.

`docker-compose.yml` mapped `5432:5432`, which Docker publishes on every
interface, and the 2026-10-14 audit found it so: `*:5433` on the owner's
machine, the compose default `jobrunner`/`jobrunner` as a superuser, and the
macOS firewall letting signed software accept connections. Anyone on the
same network could read every record, or set `owner_approved` and queue an
`apply` task the worker would then send. The API and the dashboard bind
loopback with care (`test_dashboard_binds_loopback.py`); the database they
both sit on did not.

`docker-compose.override.yml` is the owner's own, uncommitted, and remaps the
port; it is held to the same rule when it is there, since it is what runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


class _ComposeLoader(yaml.SafeLoader):
    """Compose's `!override` and `!reset` tags, read as the value they carry."""


def _tagged(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)  # type: ignore[arg-type]


_ComposeLoader.add_constructor("!override", _tagged)
_ComposeLoader.add_constructor("!reset", _tagged)


def _published(path: Path) -> list[str]:
    compose = yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader)  # noqa: S506
    ports: list[str] = []
    for service in (compose.get("services") or {}).values():
        for entry in service.get("ports") or []:
            if isinstance(entry, dict):
                ports.append(f"{entry.get('host_ip', '')}:{entry.get('published')}")
            else:
                ports.append(str(entry))
    return ports


_FILES = [
    ROOT / "docker-compose.yml",
    *(
        [ROOT / "docker-compose.override.yml"]
        if (ROOT / "docker-compose.override.yml").exists()
        else []
    ),
]


@pytest.mark.parametrize("path", _FILES, ids=lambda path: path.name)
def test_every_published_port_is_bound_to_loopback(path: Path) -> None:
    ports = _published(path)

    assert ports, f"{path.name} publishes nothing; the test would hold nothing"
    for port in ports:
        assert port.startswith("127.0.0.1:"), (
            f"{path.name} publishes {port!r} on every interface; write it as '127.0.0.1:{port}'"
        )
