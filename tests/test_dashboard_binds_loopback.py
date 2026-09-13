"""The dashboard must not put the unauthenticated API on the network.

`apps/api/middleware.py` refuses non-loopback callers, and its docstring names
one way that can be defeated: `uvicorn --proxy-headers`, which makes the app
trust an attacker-controlled header. There is a second way, it needed no header
at all, and it was the shipped default.

`next dev -H 0.0.0.0` binds the dashboard to every interface, and
`next.config.ts` rewrites `/api/:path*` to the FastAPI app. So the *client*
FastAPI sees is the Next server, on loopback, every time. The guard is looking
at the wrong end of the connection.

Reproduced on this machine before the fix, with `make api` (127.0.0.1) and
`make web` as they then were:

    GET http://127.0.0.1:8000/health         from a LAN address -> refused
    GET http://<lan-ip>:3001/api/health      from a LAN address -> 200
    GET http://<lan-ip>:3001/api/candidates  from a LAN address -> 200  (PII)
    GET http://<lan-ip>:3001/applications    from a LAN address -> 200
    POST http://<lan-ip>:3001/api/applications               -> 400, not 401

That last one is the point: a validation error means the request reached the
handler. The write path — the one that submits real job applications — was
reachable from the network, and the middleware never saw a non-local client.

`next start` was never loopback-bound either, though CLAUDE.md §3 and
`docs/USAGE.md` both said it was: it takes no `-H`, and Next passes the
undefined hostname to `server.listen(port, hostname)`, which binds every
interface.

The fix is the bind address, because that is the control that cannot be
forged. These tests hold the default rather than the mechanism.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_JSON = json.loads((ROOT / "apps/web/package.json").read_text(encoding="utf-8"))
SCRIPTS: dict[str, str] = PACKAGE_JSON["scripts"]
NEXT_CONFIG = (ROOT / "apps/web/next.config.ts").read_text(encoding="utf-8")
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")

#: Addresses that mean "every interface". `::` is the IPv6 form and binds IPv4
#: too on a dual-stack host, so listing only `0.0.0.0` would miss it.
WILDCARDS = ("0.0.0.0", "::", "[::]")

#: Sentences these documents used to assert and no longer may.
STALE_CLAIMS = (
    "`next start` is unchanged and still binds localhost",
    "next start (production) is unchanged and still binds localhost",
    "the dev server also binds `0.0.0.0`",
    "the dev server also binds 0.0.0.0",
)

#: A retraction has to be allowed to quote what it retracts — CLAUDE.md's whole
#: convention is to record what a paragraph used to say rather than edit it
#: away, and a check that forbade the words would push the next author into
#: deleting the history instead of correcting it. So an occurrence passes only
#: when one of these appears just before it.
RETRACTION_MARKERS = ("used to say", "claimed", "it never did", "no longer", "was wrong")
RETRACTION_WINDOW = 120


@pytest.mark.parametrize("script", ["dev", "start"])
def test_the_dashboard_binds_loopback_by_default(script: str) -> None:
    """Both commands, because only one of them was ever examined.

    `dev` said `-H 0.0.0.0` outright. `start` said nothing, which reads as a
    loopback default and is not one — an omitted `-H` binds everything.
    """
    command = SCRIPTS[script]
    host = re.search(r"-H\s+(\S+)", command)
    assert host, f"npm run {script} does not pass -H, and Next's default is every interface"
    assert "127.0.0.1" in host.group(1), command
    assert not any(wild in command for wild in WILDCARDS), command


@pytest.mark.parametrize("script", ["dev", "start"])
def test_the_bind_address_is_overridable_without_editing_the_file(script: str) -> None:
    """The owner had a real reason to expose it — reading the review queue from
    a phone on their own LAN. Taking that away entirely would be answered by
    editing `package.json` back, which is a change nobody reviews. One
    documented variable keeps the decision visible and the default safe."""
    assert "JOBRUNNER_WEB_HOST" in SCRIPTS[script]
    assert ":-127.0.0.1}" in SCRIPTS[script], "the fallback must be loopback"


def test_make_web_goes_through_the_npm_script() -> None:
    """One definition of the bind address. A Makefile that passed its own `-H`
    would be a second, and the two would drift the way the docs already had."""
    target = MAKEFILE.split("\nweb:", 1)[1].split("\n\n", 1)[0]
    assert "npm run dev" in target
    assert not any(wild in target for wild in WILDCARDS), target


def test_the_api_origin_the_dashboard_proxies_to_is_loopback() -> None:
    """The rewrite is what turns a bound-to-the-world dashboard into an open
    API, so its destination is part of this surface."""
    assert "127.0.0.1:8000" in NEXT_CONFIG
    assert "/api/:path*" in NEXT_CONFIG


@pytest.mark.parametrize(
    "document",
    ["CLAUDE.md", "docs/USAGE.md", "README.md"],
)
def test_no_document_still_promises_the_old_behaviour(document: str) -> None:
    """Two of these asserted that `next start` binds localhost. It did not.

    A wrong reassurance is worse than none: it is what a reader checks against
    before deciding whether they are exposed.
    """
    # Joined and whitespace-collapsed: the claim in CLAUDE.md was split across
    # two lines, so a per-line check read as passing while the sentence it was
    # written to catch sat there in full.
    text = " ".join((ROOT / document).read_text(encoding="utf-8").split()).lower()
    for claim in STALE_CLAIMS:
        start = 0
        while (found := text.find(claim, start)) != -1:
            preceding = text[max(0, found - RETRACTION_WINDOW) : found]
            assert any(marker in preceding for marker in RETRACTION_MARKERS), (
                f"{document} still promises the old behaviour: {claim!r}"
            )
            start = found + len(claim)


def test_the_api_still_refuses_a_non_loopback_client_directly() -> None:
    """The guard is unchanged and still the second line of defence.

    It was never wrong — it was answering a question about the wrong end of the
    connection. Left alone deliberately: with the dashboard on loopback it is
    what still refuses a direct `uvicorn --host 0.0.0.0`.
    """
    from apps.api.middleware import _is_loopback

    assert _is_loopback("127.0.0.1")
    assert _is_loopback("::1")
    assert not _is_loopback("192.0.2.2")
    assert not _is_loopback("10.0.0.4")
