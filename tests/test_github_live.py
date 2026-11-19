"""Live project evidence is pinned, bounded, read-only and tied to an imported repo."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import httpx
import pytest

from packages.core.models import Project
from packages.github.client import GitHubClient, GitHubError, RateLimited
from packages.github.live import read_live_project

SHA = "a" * 40
REPO = {
    "id": 17,
    "name": "example",
    "full_name": "owner/example",
    "html_url": "https://github.com/owner/example",
    "language": "Python",
    "default_branch": "main",
    "private": False,
    "fork": False,
}


def project(**changes):
    return Project(
        **(
            dict(
                id=uuid.uuid4(),
                source="github",
                external_id="17",
                full_name="owner/example",
                name="example",
                candidate_id=uuid.uuid4(),
                url=REPO["html_url"],
                is_private=False,
                is_fork=False,
                include=None,
                language="Rust",
                dependency_technologies=["obsolete"],
            )
            | changes
        )
    )


def handler_factory(*, repo=None, raw_status=200, truncated=False, paths=None):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.host in {"api.github.com", "raw.githubusercontent.com"}
        if request.url.host == "raw.githubusercontent.com":
            assert f"/{SHA}/" in request.url.path, "files must not follow a moving branch"
            return httpx.Response(raw_status, text="fastapi\n")
        if "/git/trees/" in request.url.path:
            assert request.url.path.endswith(SHA)
            return httpx.Response(
                200,
                json={
                    "tree": paths or [{"type": "blob", "path": "requirements.txt"}],
                    "truncated": truncated,
                },
            )
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": SHA})
        return httpx.Response(200, json=repo or REPO)

    return handler, requests


async def test_latest_commit_replaces_stale_technology_evidence_without_database_writes():
    original = project()
    handler, requests = handler_factory()
    session = AsyncMock()
    session.get.return_value = original
    evidence = await read_live_project(
        session,
        original.id,
        github=GitHubClient(token="synthetic-token", transport=httpx.MockTransport(handler)),
    )
    assert evidence.commit_sha == SHA and evidence.complete
    assert evidence.language == "Python" and evidence.technologies == ["FastAPI"]
    assert evidence.files_read == ["requirements.txt"]
    assert "synthetic-token" not in evidence.model_dump_json()
    assert original.language == "Rust" and original.dependency_technologies == ["obsolete"]
    session.commit.assert_not_called()
    assert len(requests) == 4


@pytest.mark.parametrize(
    "changes",
    [
        {"is_private": True},
        {"is_fork": True},
        {"include": False},
        {"full_name": "https://evil.test/repo"},
        {"full_name": "owner/.."},
    ],
)
async def test_ineligible_projects_are_refused_before_network_access(changes):
    session = AsyncMock()
    session.get.return_value = project(**changes)

    def forbidden(request):
        raise AssertionError("An excluded project must not reach GitHub")

    with pytest.raises(GitHubError, match="Only imported"):
        await read_live_project(
            session,
            session.get.return_value.id,
            github=GitHubClient(transport=httpx.MockTransport(forbidden)),
        )


@pytest.mark.parametrize("repo", [REPO | {"private": True}, REPO | {"id": 999}])
async def test_changed_visibility_or_identity_is_not_treated_as_fresh_evidence(repo):
    session = AsyncMock()
    session.get.return_value = project()
    handler, requests = handler_factory(repo=repo)
    with pytest.raises(GitHubError, match="identity|visibility"):
        await read_live_project(
            session,
            session.get.return_value.id,
            github=GitHubClient(transport=httpx.MockTransport(handler)),
        )
    assert len(requests) == 1


@pytest.mark.parametrize("options", [{"raw_status": 404}, {"truncated": True}])
async def test_partial_reads_are_labeled_and_never_fill_from_cached_technologies(options):
    session = AsyncMock()
    session.get.return_value = project()
    handler, _ = handler_factory(**options)
    evidence = await read_live_project(
        session,
        session.get.return_value.id,
        github=GitHubClient(transport=httpx.MockTransport(handler)),
    )
    assert not evidence.complete
    assert "obsolete" not in evidence.technologies


@pytest.mark.parametrize("limit", ["MAX_MANIFESTS", "MAX_MANIFEST_BYTES", "MAX_EVIDENCE_BYTES"])
async def test_file_and_byte_budgets_report_partial_coverage(limit, monkeypatch):
    import packages.github.live as live

    monkeypatch.setattr(live, limit, 1)
    session = AsyncMock()
    session.get.return_value = project()
    handler, requests = handler_factory(
        paths=[
            {"type": "blob", "path": "requirements.txt"},
            {"type": "blob", "path": "app/requirements.txt"},
        ]
    )
    evidence = await read_live_project(
        session,
        session.get.return_value.id,
        github=GitHubClient(transport=httpx.MockTransport(handler)),
    )
    assert not evidence.complete
    if limit != "MAX_MANIFESTS":
        assert not evidence.technologies
    assert len(requests) <= 5


async def test_rate_limits_remain_failures_not_an_empty_technology_list():
    session = AsyncMock()
    session.get.return_value = project()
    with pytest.raises(RateLimited):
        await read_live_project(
            session,
            session.get.return_value.id,
            github=GitHubClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(403, headers={"x-ratelimit-remaining": "0"})
                )
            ),
        )


async def test_the_api_refuses_a_project_it_cannot_check(client) -> None:
    """The jobrunner MCP tool reaches the live check through this route, like every tool."""
    response = await client.get(f"/projects/{uuid.uuid4()}/live-evidence")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert "public non-fork GitHub projects" in response.json()["error"]["message"]


async def test_the_api_reports_a_rate_limit_as_one(client, monkeypatch) -> None:
    from apps.api.routers import projects as route

    monkeypatch.setattr(route, "read_live_project", AsyncMock(side_effect=RateLimited("quota")))

    response = await client.get(f"/projects/{uuid.uuid4()}/live-evidence")

    assert response.json()["error"]["code"] == "rate_limited"
    assert "No cached evidence was substituted" in response.json()["error"]["message"]
