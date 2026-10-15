"""A page on another site must not reach the API through the owner's browser.

The API trusts any caller on loopback and has no authentication. DNS rebinding
points a domain the attacker owns at 127.0.0.1: the browser connects from this
machine, so the socket peer is loopback, and it treats the page as same-origin
with whatever answers, so it may read the replies and send JSON. One request,
`POST /applications/{id}/review {"approve": true}`, releases an application
for submission without the owner.

What the browser will not change is the `Host` it sends, which is the
attacker's domain. This app is reached by `localhost` or an IP address and
never by a domain name, so a `Host` (or the `X-Forwarded-Host` the dashboard's
proxy adds) naming a domain is refused, and so is a write whose `Origin` does.
Found by the 2026-10-14 audit; the dashboard's half is in
`apps/web/src/middleware.ts`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.middleware import _is_local_name

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "localhost:8000",
        "127.0.0.1:8000",
        "[::1]:8000",
        "192.168.1.20:3001",
        "LOCALHOST:8000",
        None,
        "",
    ],
)
def test_this_machine_is_named_by_localhost_or_an_address(host: str | None) -> None:
    assert _is_local_name(host)


@pytest.mark.parametrize(
    "host",
    [
        "evil.example:8000",
        "evil.example",
        "localhost.evil.example:8000",
        "127.0.0.1.nip.io:8000",
        "test",
        "localhost.",
    ],
)
def test_a_domain_name_is_refused(host: str) -> None:
    assert not _is_local_name(host)


async def _ask(method: str, path: str, *, headers: dict[str, str], body: object = None):
    from apps.api.main import app

    transport = ASGITransport(app=app, client=("127.0.0.1", 40000))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        return await client.request(method, path, headers=headers, json=body)


async def test_a_rebound_host_is_refused_before_any_route() -> None:
    reply = await _ask("GET", "/health", headers={"Host": "evil.example:8000"})

    assert reply.status_code == 401
    assert reply.json()["error"]["code"] == "unauthorized"


async def test_the_dashboard_proxy_is_judged_by_the_host_it_was_asked_for() -> None:
    """Next rewrites `Host` to the API's own address and puts the browser's in
    `X-Forwarded-Host` (`changeOrigin: true`), so `Host` alone would pass."""
    reply = await _ask(
        "GET",
        "/health",
        headers={"Host": "127.0.0.1:8000", "X-Forwarded-Host": "evil.example:3001"},
    )

    assert reply.status_code == 401


async def test_a_write_from_another_origin_is_refused() -> None:
    reply = await _ask(
        "POST",
        "/applications/00000000-0000-0000-0000-000000000000/review",
        headers={"Origin": "https://evil.example"},
        body={"approve": True},
    )

    assert reply.status_code == 401
    assert reply.json()["error"]["code"] == "unauthorized"


async def test_a_write_from_a_sandboxed_page_is_refused() -> None:
    reply = await _ask("POST", "/crawl", headers={"Origin": "null"})

    assert reply.status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Host": "127.0.0.1:8000"},
        {"Host": "127.0.0.1:8000", "X-Forwarded-Host": "localhost:3001"},
        {"Origin": "http://localhost:3001"},
        {"Origin": "http://127.0.0.1:3001"},
    ],
)
async def test_this_machine_still_gets_through(headers: dict[str, str]) -> None:
    reply = await _ask("GET", "/health", headers=headers)

    assert reply.status_code == 200


def _node_strips_types(node: str) -> bool:
    version = subprocess.run([node, "--version"], capture_output=True, text=True, check=False)
    major, minor, *_ = (int(part) for part in version.stdout.strip().lstrip("v").split("."))
    return (major, minor) >= (22, 18)


#: Run through node rather than restated in Python: the rule that guards the
#: dashboard is this file, and a copy here would be a second one to drift.
_DASHBOARD_RULE = ROOT / "apps/web/src/lib/local-host.ts"


def test_the_dashboard_applies_the_same_rule() -> None:
    node = shutil.which("node")
    if node is None or not _node_strips_types(node):
        pytest.skip("needs node 22.18+, which runs TypeScript as is; CI's dashboard job has it")
    hosts = [
        "localhost:3001",
        "127.0.0.1:3001",
        "[::1]:3001",
        "192.168.1.20:3001",
        "evil.example:3001",
        "localhost.evil.example:3001",
        "localhost.",
    ]
    origins = ["http://localhost:3001", "null", "https://evil.example"]
    script = (
        f"import {{ isLocalName, isLocalOrigin }} from {json.dumps(str(_DASHBOARD_RULE))};"
        f"console.log(JSON.stringify([{json.dumps(hosts)}.map(isLocalName),"
        f" {json.dumps(origins)}.map(isLocalOrigin)]));"
    )
    run = subprocess.run(
        [node, "--input-type=module", "--no-warnings", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )

    by_host, by_origin = json.loads(run.stdout)
    assert by_host == [True, True, True, True, False, False, False]
    assert by_origin == [True, False, False]


def test_the_dashboard_runs_the_rule_on_every_request() -> None:
    """Next only runs a middleware it finds at this path, and one it does not
    run guards nothing: the pages render the owner's records and its server
    actions call the API from loopback, where the API's own check passes."""
    middleware = (ROOT / "apps/web/src/middleware.ts").read_text(encoding="utf-8")

    assert 'from "@/lib/local-host"' in middleware
    assert "isLocalName(" in middleware
    assert "isLocalOrigin(" in middleware
    # A `config` export is where Next reads a matcher, and a matcher would
    # leave the paths it does not name unguarded.
    assert "export const config" not in middleware
