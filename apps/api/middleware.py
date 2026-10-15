"""Enforce the localhost-only promise at the app, not the launch command.

CLAUDE.md §1 — this runs entirely on localhost, for one user, with no auth
layer. `make api` binds 127.0.0.1, but nothing stops someone from starting
uvicorn with `--host 0.0.0.0`, which would put an unauthenticated API that can
submit real job applications on the network.

So the app refuses non-loopback callers itself. Set ALLOW_NON_LOCAL=true only
if you have deliberately put your own authentication in front of it.

Caveat: the check reads the socket peer address, which cannot be forged — but
running uvicorn with `--proxy-headers` makes it trust X-Forwarded-For instead,
and that header *is* attacker-controlled. Do not combine `--proxy-headers`
with this guard and expect it to hold.

**A second caveat, found the hard way, and it needs no misconfiguration at
all.** The peer address is the peer's, not the *user's*. Anything that proxies
to this app is the peer, so a proxy reachable from the network makes this app
reachable from the network while every request still arrives from 127.0.0.1.

That was the shipped state: the dashboard bound `0.0.0.0` and
`apps/web/next.config.ts` rewrites `/api/:path*` here, so a LAN client could
read candidate records and reach `POST /applications` — which answered 400 for
a malformed body rather than 401, meaning it had reached the handler. This
guard behaved perfectly throughout; it was answering a question about the
dashboard.

The fix is over there — both dashboard scripts bind `127.0.0.1` now
(CLAUDE.md §3). What this docstring can usefully say is the general rule: **the
bind address of anything in front of this app is part of this app's exposure**,
and no check inside a request can see it.

**A third, and the peer check cannot see it either** (2026-10-15). DNS
rebinding points a domain an attacker owns at 127.0.0.1. A page on that
domain, open in the owner's browser, connects from this machine and counts as
same-origin with whatever answers, so it can read replies and send JSON:
`POST /applications/{id}/review {"approve": true}` releases a submission. The
browser does not let the page change the `Host` it sends, and that names the
attacker's domain. This app is reached by `localhost` or an IP address, never
by a domain name, so such a `Host` is refused. So is the `X-Forwarded-Host`
the dashboard's proxy adds, which carries the browser's `Host` once Next has
rewritten its own, and so is a write whose `Origin` names a domain. The
dashboard runs the same rule on its pages (`apps/web/src/middleware.ts`).
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from packages.core.enums import ErrorCode


def _is_loopback(host: str | None) -> bool:
    if not host:
        # No client host at all — ASGI test transports do this. Treat as local.
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Unix socket or a hostname; uvicorn reports loopback as an IP, so
        # anything unparseable is not something we can vouch for.
        return host in {"localhost", "testclient", "testserver"}


def _is_local_name(host: str | None) -> bool:
    """Whether a `Host` value names this machine: `localhost` or an address.

    A missing one is not a browser's, which always sends it.
    """
    if not host:
        return True
    try:
        name = urlsplit(f"//{host}").hostname
    except ValueError:
        return False
    if name is None:
        return False
    if name == "localhost":
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def _is_local_origin(origin: str) -> bool:
    """`null`, sent by a sandboxed or file page, names nothing and is refused."""
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    return bool(parts.scheme and parts.netloc) and _is_local_name(parts.netloc)


#: Methods a page cannot send cross-site without the server's say, and the
#: ones that change something. An `Origin` on a read is the browser's business.
_WRITES = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _refused(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"error": {"code": ErrorCode.UNAUTHORIZED.value, "message": message}},
    )


class LocalhostOnlyMiddleware(BaseHTTPMiddleware):
    """Reject requests that did not come from this machine."""

    def __init__(self, app: object, *, allow_non_local: bool = False) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.allow_non_local = allow_non_local

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if self.allow_non_local:
            return await call_next(request)

        host = request.client.host if request.client else None
        if not _is_loopback(host):
            return _refused(
                "jobrunner accepts local connections only. "
                "Set ALLOW_NON_LOCAL=true if you have put your own "
                "authentication in front of it."
            )

        headers = request.headers
        asked_for = (headers.get("host"), headers.get("x-forwarded-host"))
        if not all(_is_local_name(name) for name in asked_for):
            # Never echoed: it is the other site's name, chosen by them.
            return _refused(
                "jobrunner answers to localhost or this machine's address, not to a domain name."
            )
        origin = headers.get("origin")
        if request.method in _WRITES and origin is not None and not _is_local_origin(origin):
            return _refused("jobrunner accepts changes from its own dashboard only.")

        return await call_next(request)
