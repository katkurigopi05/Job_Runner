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
    """Serve a recorded GitHub response. No test touches the network.

    The repository listing is keyed on the page parameter, not a call counter,
    so repeated syncs each see the full listing. A tree listing and the raw
    file host answer for the dependency read; `tree_calls` counts the former.
    """
    pages = {1: [REPO, FORK]}
    tree_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "raw.githubusercontent.com":
            return httpx.Response(200, text="fastapi\ntorch\nrequests\n")
        if "/git/trees/" in request.url.path:
            tree_calls.append(request.url.path)
            return httpx.Response(
                200, json={"tree": [{"path": "requirements.txt", "type": "blob"}]}
            )
        page = int(request.url.params.get("page", 1))
        return httpx.Response(200, json=pages.get(page, []))

    real_init = GitHubClient.__init__

    def patched(self, token=None, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, token, **kwargs)

    monkeypatch.setattr(GitHubClient, "__init__", patched)
    return tree_calls


async def test_sync_imports_repositories(
    client: AsyncClient, complete_candidate, _fake_github
) -> None:
    r = await client.post(
        "/projects/sync/github",
        json={"candidate_id": complete_candidate["candidate_id"], "username": "octocat"},
    )

    assert r.status_code == 200
    # The fork's dependency files are not read: they are someone else's.
    assert r.json() == {
        "added": 2,
        "updated": 0,
        "total": 2,
        "dependencies_read": 1,
        "dependencies_skipped": 0,
    }


async def test_sync_is_idempotent(client: AsyncClient, complete_candidate, _fake_github) -> None:
    """Re-syncing updates in place rather than duplicating."""
    payload = {"candidate_id": complete_candidate["candidate_id"], "username": "octocat"}
    await client.post("/projects/sync/github", json=payload)
    second = await client.post("/projects/sync/github", json=payload)

    # Nothing was pushed in between, so nothing is read again.
    assert second.json() == {
        "added": 0,
        "updated": 2,
        "total": 2,
        "dependencies_read": 0,
        "dependencies_skipped": 0,
    }

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


# --------------------------------------------------------------------------
# What a repository's dependency files say, and which résumé entry it backs
# --------------------------------------------------------------------------


async def _synced(client: AsyncClient, candidate_id: str) -> dict[str, dict]:
    await client.post(
        "/projects/sync/github", json={"candidate_id": candidate_id, "username": "octocat"}
    )
    listing = await client.get("/projects", params={"candidate_id": candidate_id})
    return {p["name"]: p for p in listing.json()}


async def test_a_sync_records_what_the_dependency_files_declare(
    client: AsyncClient, complete_candidate, _fake_github
) -> None:
    projects = await _synced(client, complete_candidate["candidate_id"])

    # `requests` is in the file and not in the table, so it is not a skill.
    assert projects["jobrunner"]["dependency_technologies"] == ["FastAPI", "PyTorch"]
    assert projects["someones-lib"]["dependency_technologies"] == []
    assert len(_fake_github) == 1


async def test_a_repository_is_read_again_only_after_a_new_push(
    client: AsyncClient, complete_candidate, _fake_github
) -> None:
    await _synced(client, complete_candidate["candidate_id"])
    await _synced(client, complete_candidate["candidate_id"])
    assert len(_fake_github) == 1

    REPO["pushed_at"] = "2026-09-01T10:00:00Z"
    try:
        await _synced(client, complete_candidate["candidate_id"])
    finally:
        REPO["pushed_at"] = "2026-08-01T10:00:00Z"
    assert len(_fake_github) == 2


async def test_the_owner_links_a_repository_to_a_resume_entry_and_a_sync_keeps_it(
    client: AsyncClient, complete_candidate, _fake_github
) -> None:
    projects = await _synced(client, complete_candidate["candidate_id"])
    project_id = projects["jobrunner"]["id"]

    r = await client.patch(f"/projects/{project_id}", json={"resume_entry": "  Jobrunner  "})
    assert r.status_code == 200
    assert r.json()["resume_entry"] == "Jobrunner"

    projects = await _synced(client, complete_candidate["candidate_id"])
    assert projects["jobrunner"]["resume_entry"] == "Jobrunner"

    cleared = await client.patch(f"/projects/{project_id}", json={"resume_entry": None})
    assert cleared.json()["resume_entry"] is None


async def test_a_blank_link_clears_it_rather_than_naming_an_empty_entry(
    client: AsyncClient, complete_candidate, _fake_github
) -> None:
    projects = await _synced(client, complete_candidate["candidate_id"])
    project_id = projects["jobrunner"]["id"]
    await client.patch(f"/projects/{project_id}", json={"resume_entry": "Jobrunner"})

    r = await client.patch(f"/projects/{project_id}", json={"resume_entry": "   "})
    assert r.json()["resume_entry"] is None


async def test_a_link_too_long_to_be_an_entry_name_is_refused(
    client: AsyncClient, complete_candidate, _fake_github
) -> None:
    projects = await _synced(client, complete_candidate["candidate_id"])
    r = await client.patch(
        f"/projects/{projects['jobrunner']['id']}", json={"resume_entry": "x" * 201}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


async def test_linked_evidence_groups_by_entry_and_leaves_out_a_fork(
    client: AsyncClient, complete_candidate, _fake_github, committing_sessionmaker
) -> None:
    import uuid

    from packages.tailor.evidence import linked_evidence

    projects = await _synced(client, complete_candidate["candidate_id"])
    await client.patch(
        f"/projects/{projects['jobrunner']['id']}",
        json={"resume_entry": "Jobrunner — Local-First Job Application Agent"},
    )
    # A fork linked to the same entry contributes nothing: someone else's code.
    await client.patch(
        f"/projects/{projects['someones-lib']['id']}", json={"resume_entry": "Jobrunner"}
    )

    async with committing_sessionmaker() as session:
        linked = await linked_evidence(session, uuid.UUID(complete_candidate["candidate_id"]))

    assert list(linked) == ["jobrunner"]
    # Technology facts only: the language, topics and declared tools. Not the
    # description, whose ordinary words would be offered for rewriting.
    assert linked["jobrunner"].split("\n") == ["Python", "automation", "FastAPI", "PyTorch"]


async def test_entries_lists_the_resumes_projects_and_suggests_by_exact_name(
    client: AsyncClient, complete_candidate, _fake_github, committing_sessionmaker
) -> None:
    import uuid

    from packages.core.models import Resume

    async with committing_sessionmaker() as session:
        resume = Resume(
            candidate_id=uuid.UUID(complete_candidate["candidate_id"]),
            storage_ref="resumes/test/entries.pdf",
            version=99,
            parsed_json={
                "contact": {},
                "preamble": [],
                "sections": {
                    "projects": [
                        "jobrunner — Local agent   [GitHub]",
                        "Python, FastAPI, Postgres",
                        "Built an apply pipeline.",
                    ]
                },
                "raw_lines": [],
            },
        )
        session.add(resume)
        await session.commit()
        resume_id = resume.id

    projects = await _synced(client, complete_candidate["candidate_id"])
    r = await client.get("/projects/entries", params={"resume_id": str(resume_id)})

    assert r.status_code == 200
    assert r.json()["entries"] == ["jobrunner"]
    assert r.json()["suggestions"] == {projects["jobrunner"]["id"]: "jobrunner"}


async def test_entries_for_an_unknown_resume_is_404(client: AsyncClient) -> None:
    import uuid

    r = await client.get("/projects/entries", params={"resume_id": str(uuid.uuid4())})
    assert r.status_code == 404
