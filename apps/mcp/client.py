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

DEFAULT_BASE_URL = "http://127.0.0.1:8000"


class ApiUnavailable(Exception):
    """The Jobrunner API is not reachable."""


class ApiCallFailed(Exception):
    """The API returned an error envelope."""

    def __init__(self, code: str, message: str, status: int) -> None:
        self.code = code
        self.message = message
        self.status = status
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
        try:
            async with self._client() as client:
                response = await client.request(method, path, json=json, params=params)
        except httpx.ConnectError as exc:
            raise ApiUnavailable(
                f"Cannot reach the Jobrunner API at {self.base_url}. Start it with `make api`."
            ) from exc

        if response.status_code >= 400:
            try:
                envelope = response.json()["error"]
                raise ApiCallFailed(envelope["code"], envelope["message"], response.status_code)
            except (KeyError, ValueError) as exc:
                raise ApiCallFailed(
                    "internal_error", response.text[:500], response.status_code
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
