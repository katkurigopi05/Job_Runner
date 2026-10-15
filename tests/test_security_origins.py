"""Exercise both real origin guards without reaching a database or employer."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from apps.api.middleware import LocalhostOnlyMiddleware

ROOT = Path(__file__).resolve().parents[1]

# An address is a valid Host, but not evidence that a page belongs to this app.
CASES = [
    ("http://localhost:3001", "localhost:8000", True),
    ("http://127.0.0.1:3001", "localhost:8000", True),
    ("http://[::1]:3001", "localhost:8000", True),
    ("http://192.168.1.20:3001", "192.168.1.20:3001", True),
    ("https://192.168.1.20", "192.168.1.20:443", True),
    ("http://[fd00::20]:3001", "[fd00::20]:3001", True),
    ("https://203.0.113.7", "localhost:8000", False),
    ("http://192.168.1.21:3001", "192.168.1.20:3001", False),
    ("http://192.168.1.20:4000", "192.168.1.20:3001", False),
    ("http://[2001:db8::7]", "localhost:8000", False),
    ("https://evil.example", "localhost:8000", False),
    ("null", "localhost:8000", False),
    ("", "localhost:8000", False),
    ("file:///tmp/page.html", "localhost:8000", False),
    ("data:text/html,hello", "localhost:8000", False),
    ("javascript:alert(1)", "localhost:8000", False),
    ("blob:http://localhost/id", "localhost:8000", False),
    ("ftp://localhost", "localhost:8000", False),
    ("http://user@localhost", "localhost:8000", False),
    ("http://localhost/path", "localhost:8000", False),
    ("http://localhost?query", "localhost:8000", False),
    ("http://localhost#fragment", "localhost:8000", False),
    ("http://localhost:99999", "localhost:8000", False),
    ("http://localhost:bad", "localhost:8000", False),
    (" http://localhost", "localhost:8000", False),
    ("http://local\thost", "localhost:8000", False),
]


async def _accepted(request):
    return JSONResponse({"reached_route": True})


@pytest.mark.parametrize("origin,host,allowed", CASES)
@pytest.mark.parametrize("proxied", [False, True])
async def test_api_origin_is_checked_before_a_write(origin, host, allowed, proxied) -> None:
    app = Starlette(routes=[Route("/write", _accepted, methods=["POST"])])
    app.add_middleware(LocalhostOnlyMiddleware)
    headers = {"Origin": origin, "Host": host}
    if proxied:
        headers = {**headers, "Host": "127.0.0.1:8000", "X-Forwarded-Host": host}
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 40000)),
        base_url="http://localhost",
    ) as client:
        reply = await client.post("/write", headers=headers)
    assert reply.status_code == (200 if allowed else 401)
    if not allowed:
        assert reply.json()["error"]["code"] == "unauthorized"


def test_dashboard_origin_policy_matches_api() -> None:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("REQUIRE_SECURITY_NODE") == "1":
            pytest.fail("security gate requires Node to exercise the dashboard guard")
        pytest.skip("Node is not installed")
    module = ROOT / "apps/web/src/lib/local-host.ts"
    script = (
        f"import {{ isLocalOrigin }} from {json.dumps(str(module))};"
        f"const cases = {json.dumps(CASES)};"
        "console.log(JSON.stringify(cases.map(([origin, host]) => isLocalOrigin(origin, host))));"
    )
    result = subprocess.run(
        [node, "--input-type=module", "--no-warnings", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    actual = json.loads(result.stdout)
    for case, accepted in zip(CASES, actual, strict=True):
        assert accepted is case[2], case


def test_dashboard_supplies_the_request_host_to_the_origin_guard() -> None:
    source = (ROOT / "apps/web/src/middleware.ts").read_text()
    assert 'const host = request.headers.get("host")' in source
    assert "isLocalOrigin(origin, host)" in source
