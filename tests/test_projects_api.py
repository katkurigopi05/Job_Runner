"""Project API — sync, curation, and selection preview."""

from __future__ import annotations

import httpx
import pytest
from httpx import AsyncClient

from packages.github.client import GitHubClient

REPO = {
    "id": 1,
    "name": "jobrunner",
    "full_name": "octocat/jobrunner",
    "html_url": "https://github.com/octocat/jobrunner",
    "homepage": None,
    "description": "Local job-application agent in Python",
    "language": "Python",
    "topics": ["automation"],
    "stargazers_count": 12,
    "forks_count": 0,
    "fork": False,
    "archived": False,
    "private": False,
    "pushed_at": "2026-08-01T10:00:00Z",
}
FORK = {**REPO, "id": 2, "name": "someones-lib", "fork": True}


@pytest.fixture
def _fake_github(monkeypatch):
    """Serve a recorded GitHub response. No test touches the network."""
    # Keyed on the page parameter, not a call counter, so repeated syncs each
    # see the full listing.
    pages = {1: [REPO, FORK]}

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        return httpx.Response(200, json=pages.get(page, []))

    real_init = GitHubClient.__init__

    def patched(self, token=None, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, token, **kwargs)

    monkeypatch.setattr(GitHubClient, "__init__", patched)


async def test_sync_imports_repositories(
    client: AsyncClient, complete_candidate, _fake_github
) -> None:
    r = await client.post(
        "/projects/sync/github",
        json={"candidate_id": complete_candidate["candidate_id"], "username": "octocat"},
    )

    assert r.status_code == 200
    assert r.json() == {"added": 2, "updated": 0, "total": 2}


async def test_sync_is_idempotent(client: AsyncClient, complete_candidate, _fake_github) -> None:
    """Re-syncing updates in place rather than duplicating."""
    payload = {"candidate_id": complete_candidate["candidate_id"], "username": "octocat"}
    await client.post("/projects/sync/github", json=payload)
    second = await client.post("/projects/sync/github", json=payload)

    assert second.json() == {"added": 0, "updated": 2, "total": 2}

    listing = await client.get(
        "/projects", params={"candidate_id": complete_candidate["candidate_id"]}
    )
    assert len(listing.json()) == 2


async def test_sync_preserves_owner_curation(
    client: AsyncClient, complete_candidate, _fake_github
) -> None:
    """A sync refreshes GitHub's facts, never the owner's decisions."""
    payload = {"candidate_id": complete_candidate["candidate_id"], "username": "octocat"}
    await client.post("/projects/sync/github", json=payload)

    listing = await client.get(
        "/projects", params={"candidate_id": complete_candidate["candidate_id"]}
    )
    project_id = listing.json()[0]["id"]
    await client.patch(f"/projects/{project_id}", json={"pinned": True, "include": True})

    await client.post("/projects/sync/github", json=payload)

    refreshed = await client.get(
        "/projects", params={"candidate_id": complete_candidate["candidate_id"]}
    )
    pinned = next(p for p in refreshed.json() if p["id"] == project_id)
    assert pinned["pinned"] is True
    assert pinned["include"] is True


async def test_sync_unknown_candidate_is_invalid_request(client: AsyncClient, _fake_github) -> None:
    import uuid

    r = await client.post(
        "/projects/sync/github",
        json={"candidate_id": str(uuid.uuid4()), "username": "octocat"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


async def test_preview_excludes_forks_and_shows_the_rendered_link(
    client: AsyncClient, complete_candidate, _fake_github
) -> None:
    await client.post(
        "/projects/sync/github",
        json={"candidate_id": complete_candidate["candidate_id"], "username": "octocat"},
    )

    r = await client.post(
        "/projects/preview",
        params={
            "candidate_id": complete_candidate["candidate_id"],
            "job_text": "Python backend engineer",
        },
    )

    assert r.status_code == 200
    names = [p["name"] for p in r.json()]
    assert "jobrunner" in names
    assert "someones-lib" not in names  # a fork
    assert "github.com/octocat/jobrunner" in r.json()[0]["rendered_link"]
    assert "Python" in r.json()[0]["matched_terms"]
    assert r.json()[0]["evidence_source"] == "github_metadata"


async def test_patch_unknown_project_is_404(client: AsyncClient) -> None:
    import uuid

    r = await client.patch(f"/projects/{uuid.uuid4()}", json={"pinned": True})
    assert r.status_code == 404
