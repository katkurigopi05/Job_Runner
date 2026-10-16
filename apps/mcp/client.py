"""Thin client over the local Jobrunner API.

The MCP tools go through HTTP rather than the database directly, on purpose.
The completeness gate, the approval gate, and the state machine all live behind
the API — reaching past them would mean two code paths that can disagree about
when an application may be created or submitted. One of those paths would
eventually skip a check.

The cost is that the API has to be running. That trade is worth it: a stale
connection error is obvious, a silently divergent safety check is not.
"""

from __future__ import annotations

from typing import Any

import httpx

from packages.core.trace import TRACE_HEADER, new_trace_id, normalize_trace_id

DEFAULT_BASE_URL = "http://127.0.0.1:8000"


class ApiUnavailable(Exception):
    """The Jobrunner API is not reachable."""


class ApiDidNotAnswer(Exception):
    """The request was sent and no answer came back.

    Not the same as unreachable. A connection that was refused sent nothing;
    this one was taken, and then timed out or was dropped, which `make api`
    does to every open request each time a file is saved. For a read that
    costs nothing. For a write the caller cannot know whether it was carried
    out, and asking again could carry it out twice.
    """


class ApiCallFailed(Exception):
    """The API returned an error envelope."""

    def __init__(
        self, code: str, message: str, status: int, *, trace_id: str | None = None
    ) -> None:
        self.code = code
        self.message = message
        self.status = status
        self.trace_id = trace_id
        super().__init__(f"{code}: {message}")


class JobrunnerClient:
    """Calls the local API. Injectable so tests can bind it to the ASGI app."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url, transport=self._transport, timeout=self._timeout
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        trace_id = new_trace_id()
        try:
            async with self._client() as client:
                response = await client.request(
                    method, path, json=json, params=params, headers={TRACE_HEADER: trace_id}
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ApiUnavailable(
                f"Cannot reach the Jobrunner API at {self.base_url}. Start it with `make api`."
            ) from exc
        except httpx.TransportError as exc:
            # httpx's timeouts carry no message, so the tool failed with
            # nothing after "Error executing tool approve_application:".
            waited = (
                f"within {self._timeout:g} s"
                if isinstance(exc, httpx.TimeoutException)
                else "before the connection closed"
            )
            outcome = (
                "Nothing was changed: a read can be asked again."
                if method.upper() == "GET"
                else "The request was sent and may have been carried out. Look at what it "
                "was meant to change before asking again, so it is not done twice."
            )
            raise ApiDidNotAnswer(
                f"The Jobrunner API at {self.base_url} did not answer {waited}. {outcome} "
                f"Trace ID: {trace_id}."
            ) from exc

        if response.status_code >= 400:
            response_trace = normalize_trace_id(response.headers.get(TRACE_HEADER)) or trace_id
            try:
                envelope = response.json()["error"]
                raise ApiCallFailed(
                    envelope["code"],
                    envelope["message"],
                    response.status_code,
                    trace_id=response_trace,
                )
            except (KeyError, ValueError) as exc:
                raise ApiCallFailed(
                    "internal_error",
                    response.text[:500],
                    response.status_code,
                    trace_id=response_trace,
                ) from exc

        if not response.content:
            return None
        return response.json()

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params or None)

    async def post(self, path: str, json: Any = None, **params: Any) -> Any:
        return await self.request("POST", path, json=json, params=params or None)

    async def patch(self, path: str, json: Any = None) -> Any:
        return await self.request("PATCH", path, json=json)

    async def health(self) -> bool:
        try:
            await self.get("/health")
        except (ApiUnavailable, ApiCallFailed):
            return False
        return True
