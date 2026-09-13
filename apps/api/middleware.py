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
"""

from __future__ import annotations

import ipaddress

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
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "code": ErrorCode.UNAUTHORIZED.value,
                        "message": (
                            "jobrunner accepts local connections only. "
                            "Set ALLOW_NON_LOCAL=true if you have put your own "
                            "authentication in front of it."
                        ),
                    }
                },
            )
        return await call_next(request)
