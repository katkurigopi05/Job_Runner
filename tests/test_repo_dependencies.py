"""What a repository's own dependency files say it uses.

No test touches the network. GitHub's tree listing and the raw file host are
served by a mock transport.
"""

from __future__ import annotations

import zlib
from datetime import UTC, datetime

import httpx
import pytest

from packages.github.client import GitHubClient, RateLimited, Repository
from packages.github.manifests import (
    TECHNOLOGY_NAMES,
    is_manifest_path,
    packages_in,
    technologies,
)
from packages.github.sync import repos_to_read

# --------------------------------------------------------------------------
# Which files are read
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "requirements.txt",
        "backend/requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
        "frontend/package.json",
        "packages/editor-state/package.json",
        "src-tauri/Cargo.toml",
    ],
)
def test_a_dependency_file_is_read(path: str) -> None:
    assert is_manifest_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "node_modules/react/package.json",
        "frontend/node_modules/vite/package.json",
        ".venv/lib/python3.12/site-packages/torch/pyproject.toml",
        "vendor/lib/Cargo.toml",
        "tests/fixtures/package.json",
        "README.md",
        "package-lock.json",
    ],
)
def test_vendored_fixture_and_lock_files_are_not(path: str) -> None:
    assert not is_manifest_path(path)


# --------------------------------------------------------------------------
# What each file declares
# --------------------------------------------------------------------------


def test_requirements_names_without_versions_extras_or_comments() -> None:
    text = """
# core
torch>=2.1
llama-index[openai]==0.10.1  # retrieval
scikit_learn
-r other.txt
-e git+https://example.com/x.git#egg=x
--index-url https://example.com
"""
    assert packages_in("requirements.txt", text) == {"torch", "llama-index", "scikit-learn"}


def test_pyproject_reads_pep621_optional_and_poetry_dependencies() -> None:
    text = """
[project]
dependencies = ["fastapi>=0.110", "pydantic"]

[project.optional-dependencies]
ml = ["sentence-transformers"]

[tool.poetry.dependencies]
python = "^3.12"
streamlit = "*"
"""
    assert packages_in("pyproject.toml", text) == {
        "fastapi",
        "pydantic",
        "sentence-transformers",
        "streamlit",
    }


def test_package_json_reads_dependencies_and_dev_dependencies() -> None:
    text = (
        '{"dependencies": {"react": "^18"},'
        ' "devDependencies": {"vitest": "^1", "@playwright/test": "^1"}}'
    )
    assert packages_in("package.json", text) == {"react", "vitest", "@playwright/test"}


def test_cargo_reads_dependencies() -> None:
    text = (
        '[dependencies]\ntauri = "2"\nserde = { version = "1" }\n[dev-dependencies]\ntokio = "1"\n'
    )
    assert packages_in("Cargo.toml", text) == {"tauri", "serde", "tokio"}


def test_a_file_that_does_not_parse_declares_nothing() -> None:
    assert packages_in("package.json", "{not json") == set()
    assert packages_in("pyproject.toml", "[[[") == set()


# --------------------------------------------------------------------------
# From package to the name a résumé uses
# --------------------------------------------------------------------------


def test_packages_map_to_the_names_a_resume_uses() -> None:
    assert technologies({"torch", "llama-index-core", "google-generativeai", "vitest"}) == [
        "Gemini API",
        "LlamaIndex",
        "PyTorch",
        "Vitest",
    ]


def test_a_package_not_in_the_table_is_ignored_not_guessed() -> None:
    assert technologies({"left-pad", "my-internal-utils"}) == []


def test_the_table_names_no_package_twice_and_no_empty_name() -> None:
    assert all(name.strip() for name in TECHNOLOGY_NAMES.values())
    assert all(key == key.lower() for key in TECHNOLOGY_NAMES)


# --------------------------------------------------------------------------
# Reading a repository
# --------------------------------------------------------------------------

PUSHED = datetime(2026, 10, 1, tzinfo=UTC)


def _repo(name: str = "Attorney.AI", **overrides: object) -> Repository:
    payload = {
        "id": zlib.crc32(name.encode()),
        "name": name,
        "full_name": f"octocat/{name}",
        "html_url": f"https://github.com/octocat/{name}",
        "default_branch": "main",
        "fork": False,
        "pushed_at": PUSHED.isoformat(),
    }
    payload.update(overrides)
    return Repository.from_api(payload)


def _github(files: dict[str, str], *, tree_status: int = 200, calls: list[str] | None = None):
    """A tree listing of `files`, and their contents on the raw host."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(f"{request.url.host}{request.url.path}")
        if request.url.host == "raw.githubusercontent.com":
            path = request.url.path.split("/main/", 1)[1]
            if path in files:
                return httpx.Response(200, text=files[path])
            return httpx.Response(404)
        if "/git/trees/" in request.url.path:
            if tree_status != 200:
                return httpx.Response(
                    tree_status, headers={"x-ratelimit-remaining": "0"}, json={"message": "limit"}
                )
            tree = [{"path": path, "type": "blob"} for path in files]
            return httpx.Response(200, json={"tree": tree, "truncated": False})
        return httpx.Response(404)

    return GitHubClient(transport=httpx.MockTransport(handler))


async def test_a_repository_is_read_for_what_it_uses() -> None:
    client = _github(
        {
            "backend/requirements.txt": "llama-index\nqdrant-client\nsupabase\n",
            "frontend/package.json": '{"dependencies": {"react": "^18"}}',
            "README.md": "torch is mentioned here but not declared",
        }
    )
    assert await client.repository_technologies(_repo()) == [
        "LlamaIndex",
        "Qdrant",
        "React",
        "Supabase",
    ]


async def test_a_rate_limit_is_raised_not_read_as_no_dependencies() -> None:
    client = _github({"requirements.txt": "torch\n"}, tree_status=403)
    with pytest.raises(RateLimited):
        await client.repository_technologies(_repo())


async def test_an_unexpected_tree_shape_reads_as_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"not": "a tree"}])

    client = GitHubClient(transport=httpx.MockTransport(handler))
    assert await client.repository_technologies(_repo()) == []


async def test_file_contents_come_from_the_raw_host_not_the_api() -> None:
    calls: list[str] = []
    client = _github({"requirements.txt": "torch\n"}, calls=calls)
    await client.repository_technologies(_repo())
    api_calls = [call for call in calls if call.startswith("api.github.com")]
    assert len(api_calls) == 1  # the tree listing; the file itself is not an API call


def test_forks_and_repos_unchanged_since_their_last_read_are_not_read_again() -> None:
    fork = _repo("someones-lib", fork=True)
    fresh = _repo("photo-editor")
    unchanged = _repo("color-harmony")
    pushed_again = _repo("CALFIRE")
    last_read = {
        unchanged.external_id: PUSHED,
        pushed_again.external_id: datetime(2026, 9, 1, tzinfo=UTC),
    }
    chosen = repos_to_read([fork, fresh, unchanged, pushed_again], last_read)
    assert [r.name for r in chosen] == ["photo-editor", "CALFIRE"]
