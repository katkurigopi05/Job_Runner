"""Read the owner's own repositories from the GitHub API.

Scope is deliberately narrow: this fetches repositories belonging to one
account — the owner's — and nothing else. It is not a crawler and does not
follow links off GitHub.

Everything it returns is *reported by GitHub verbatim*. Nothing here
summarizes, rewrites, or infers what a project does. A repository with no
description comes back with no description; §2.1 makes inventing one a bug,
not a nicety.

Unauthenticated requests are limited to 60/hour. Set GITHUB_TOKEN (a
read-only, public_repo-scoped token is enough) for 5000/hour and to include
private repositories.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, Field

log = structlog.get_logger(__name__)

API_ROOT = "https://api.github.com"

#: GitHub caps this at 100.
PAGE_SIZE = 100

#: Refuse to page forever if the account is enormous.
MAX_PAGES = 10


class GitHubError(Exception):
    """The API call failed or was refused."""


class RateLimited(GitHubError):
    """Out of API quota. Unauthenticated is 60/hour."""


class Repository(BaseModel):
    """One repository, exactly as GitHub reported it."""

    external_id: str
    name: str
    full_name: str
    url: str
    homepage: str | None = None
    description: str | None = None
    language: str | None = None
    topics: list[str] = Field(default_factory=list)
    stars: int = 0
    forks: int = 0
    is_fork: bool = False
    is_archived: bool = False
    is_private: bool = False
    pushed_at: datetime | None = None

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> Repository:
        return cls(
            external_id=str(payload["id"]),
            name=payload["name"],
            full_name=payload.get("full_name") or payload["name"],
            url=payload["html_url"],
            homepage=payload.get("homepage") or None,
            description=payload.get("description") or None,
            language=payload.get("language") or None,
            topics=list(payload.get("topics") or []),
            stars=payload.get("stargazers_count", 0) or 0,
            forks=payload.get("forks_count", 0) or 0,
            is_fork=bool(payload.get("fork")),
            is_archived=bool(payload.get("archived")),
            is_private=bool(payload.get("private")),
            pushed_at=payload.get("pushed_at"),
        )


class GitHubClient:
    """Minimal read-only client. One account, repositories only."""

    def __init__(
        self,
        token: str | None = None,
        *,
        api_root: str = API_ROOT,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.api_root = api_root.rstrip("/")
        self.token = token
        self._transport = transport
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "jobrunner",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def list_repositories(
        self, username: str, *, include_private: bool = False
    ) -> list[Repository]:
        """Every repository for `username`, newest push first.

        With a token, `/user/repos` is used so private repositories are
        visible; without one, only the public listing is available.
        """
        if include_private and not self.token:
            raise GitHubError("include_private requires a GITHUB_TOKEN")

        path = "/user/repos" if include_private else f"/users/{username}/repos"
        repositories: list[Repository] = []

        async with httpx.AsyncClient(
            transport=self._transport, timeout=self._timeout, headers=self._headers()
        ) as client:
            for page in range(1, MAX_PAGES + 1):
                params: dict[str, Any] = {
                    "per_page": PAGE_SIZE,
                    "page": page,
                    "sort": "pushed",
                    "direction": "desc",
                }
                if include_private:
                    params["affiliation"] = "owner"
                else:
                    params["type"] = "owner"

                response = await client.get(f"{self.api_root}{path}", params=params)
                self._raise_for_status(response)

                batch = response.json()
                if not isinstance(batch, list):
                    raise GitHubError("unexpected response shape from GitHub")
                repositories.extend(Repository.from_api(item) for item in batch)

                if len(batch) < PAGE_SIZE:
                    break

        log.info("github_repos_fetched", username=username, count=len(repositories))
        return repositories

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code == 200:
            return
        if response.status_code in (403, 429):
            remaining = response.headers.get("x-ratelimit-remaining")
            if remaining == "0":
                raise RateLimited(
                    "GitHub API rate limit exhausted. Set GITHUB_TOKEN to raise "
                    "the limit from 60/hour to 5000/hour."
                )
            raise GitHubError(f"GitHub refused the request ({response.status_code})")
        if response.status_code == 404:
            raise GitHubError("GitHub account or endpoint not found")
        if response.status_code == 401:
            raise GitHubError("GITHUB_TOKEN was rejected")
        raise GitHubError(f"GitHub returned {response.status_code}")
