"""Private repositories: listed only with a token, printed only once the owner says so.

And what a repository's container and CI files declare, read under a byte
budget. No test reaches GitHub: every request is answered by a MockTransport.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest

from packages.core.models import Project
from packages.core.schemas import SyncGitHubRequest
from packages.github import client as github_client
from packages.github.client import GitHubClient, Repository
from packages.github.infrastructure import declared_in, is_infrastructure_path
from packages.github.select import is_eligible, select_projects
from packages.github.sync import sync_from_github
from packages.tailor.projects import LinkStyle, ProjectEntry, link_text, render_entry_html

TOKEN = "ghp_" + "x" * 36

REPO = {
    "id": 7,
    "name": "harness",
    "full_name": "octocat/harness",
    "html_url": "https://github.com/octocat/harness",
    "description": "Evaluation harness for agent tool calls",
    "language": "Python",
    "topics": ["agents"],
    "private": True,
    "pushed_at": "2026-11-01T10:00:00Z",
    "default_branch": "main",
    "owner": {"login": "octocat"},
}


def _client(handler, token: str | None = None) -> GitHubClient:  # noqa: ANN001
    return GitHubClient(token=token, transport=httpx.MockTransport(handler))


async def test_a_token_lists_private_and_collaborator_repositories() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[REPO])

    repositories = await _client(handler, TOKEN).list_repositories("octocat", include_private=True)

    assert seen[0].url.path == "/user/repos"
    assert seen[0].url.params["affiliation"] == "owner,collaborator"
    assert seen[0].url.params["visibility"] == "all"
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert repositories[0].is_private and repositories[0].owner_login == "octocat"


async def test_asking_for_private_without_a_token_reads_the_public_listing_and_says_so(
    db_session,
) -> None:
    from packages.core.models import Candidate, User

    user = User(email=f"{uuid.uuid4().hex}@example.com")
    db_session.add(user)
    await db_session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email="owner@example.com")
    db_session.add(candidate)
    await db_session.flush()

    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if "/git/trees/" in request.url.path:
            return httpx.Response(200, json={"tree": []})
        return httpx.Response(200, json=[{**REPO, "private": False}])

    result = await sync_from_github(
        db_session, candidate.id, "octocat", include_private=True, client=_client(handler)
    )

    assert paths[0] == "/users/octocat/repos"
    assert not result.private_included and result.private == 0 and result.total == 1


def test_the_token_is_kept_out_of_the_request_repr() -> None:
    body = SyncGitHubRequest(candidate_id=uuid.uuid4(), username="octocat", token=TOKEN)
    assert TOKEN not in repr(body)


@pytest.mark.parametrize(
    ("path", "text", "expected"),
    [
        ("Dockerfile", "FROM python:3.12-slim\nRUN pip install x\n", {"Docker"}),
        ("deploy/api.Dockerfile", "FROM nvidia/cuda:12.4.1-runtime\n", {"Docker", "CUDA"}),
        (
            "docker-compose.yml",
            "services:\n  db:\n    image: pgvector/pgvector:pg16\n  cache:\n    image: redis:7\n",
            {"Docker", "Docker Compose", "pgvector", "Redis"},
        ),
        (
            ".github/workflows/ci.yml",
            "jobs:\n  test:\n    steps:\n      - uses: actions/checkout@v4\n"
            "      - uses: aws-actions/configure-aws-credentials@v4\n"
            "      - run: kubectl apply -f k8s/\n",
            {"GitHub Actions", "AWS"},
        ),
        ("Dockerfile", "# FROM postgres in a comment only\n", set()),
        (
            "docker-compose.yml",
            "services:\n  app:\n    image: someone/unknown-tool\n",
            {"Docker", "Docker Compose"},
        ),
    ],
)
def test_container_and_ci_files_declare_only_what_the_tables_name(
    path: str, text: str, expected: set[str]
) -> None:
    """A `run:` line naming kubectl is a script, not a declaration, so it adds nothing."""
    assert declared_in(path, text) == expected


def test_vendored_and_installed_files_are_not_the_projects() -> None:
    assert is_infrastructure_path("Dockerfile")
    assert is_infrastructure_path(".github/workflows/release.yaml")
    assert not is_infrastructure_path("node_modules/pkg/Dockerfile")
    assert not is_infrastructure_path("vendor/thing/docker-compose.yml")
    assert not is_infrastructure_path("docs/workflows/ci.yml")


async def test_dependency_container_and_ci_evidence_are_read_together() -> None:
    files = {
        "requirements.txt": "fastapi\nrequests\n",
        "Dockerfile": "FROM python:3.12\n",
        ".github/workflows/ci.yml": (
            "jobs:\n  t:\n    steps:\n      - uses: docker/build-push-action@v6\n"
        ),
        "README.md": "We compared Kubernetes and Nomad.",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "/git/trees/" in request.url.path:
            tree = [{"path": path, "type": "blob"} for path in files]
            return httpx.Response(200, json={"tree": tree})
        path = request.url.path.split("/main/", 1)[1]
        return httpx.Response(200, text=files[path])

    repository = Repository.from_api(REPO)
    found = await _client(handler, TOKEN).repository_technologies(repository)

    assert found == ["Docker", "FastAPI", "GitHub Actions"]


async def test_the_byte_budget_stops_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_client, "MAX_EVIDENCE_BYTES", 30)
    served: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/git/trees/" in request.url.path:
            tree = [
                {"path": "Dockerfile", "type": "blob"},
                {"path": "a/requirements.txt", "type": "blob"},
            ]
            return httpx.Response(200, json={"tree": tree})
        served.append(request.url.path)
        body = "FROM python:3.12\n" if request.url.path.endswith("Dockerfile") else "torch\n" * 20
        return httpx.Response(200, text=body)

    found = await _client(handler, TOKEN).repository_technologies(Repository.from_api(REPO))

    assert found == ["Docker"] and len(served) == 2


def _project(**fields: object) -> Project:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "name": "harness",
        "url": "https://github.com/octocat/harness",
        "description": "Evaluation harness",
        "language": "Python",
        "topics_json": [],
        "stars": 0,
        "is_fork": False,
        "is_archived": False,
        "is_private": True,
        "include": None,
        "pinned": False,
        "pushed_at": datetime.now(UTC),
    }
    values.update(fields)
    return Project(**values)


@pytest.mark.parametrize(
    ("include", "pinned", "listed"),
    [(None, False, False), (True, False, True), (None, True, True), (False, True, False)],
)
def test_a_private_repository_is_printed_only_once_included_or_pinned(
    include: bool | None, pinned: bool, listed: bool
) -> None:
    project = _project(include=include, pinned=pinned)
    assert project.resume_listed is listed
    assert is_eligible(project) is listed
    assert (select_projects([project], "python agents") == [project]) is listed


def test_a_public_repository_is_unaffected() -> None:
    assert _project(is_private=False).resume_listed


def test_a_private_repository_never_prints_its_address() -> None:
    entry = ProjectEntry.from_project(_project(include=True))
    for style in LinkStyle:
        assert link_text(entry, style) == ""
    rendered = render_entry_html(entry)
    assert "href" not in rendered and "github.com" not in rendered and "harness" in rendered

    public = ProjectEntry.from_project(_project(is_private=False))
    assert 'href="https://github.com/octocat/harness"' in render_entry_html(public)
