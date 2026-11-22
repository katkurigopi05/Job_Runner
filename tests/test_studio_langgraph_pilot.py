"""PostgreSQL restart, retry, parity, and privacy tests for the LangGraph Studio pilot.

Every test runs offline against the guarded test PostgreSQL database and a local
loopback fake GitHub HTTP server (no live GitHub, no model credentials).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import select
import subprocess
import sys
import threading
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import psycopg
import pytest
import pytest_asyncio
from psycopg.rows import dict_row
from sqlalchemy import text
from sqlalchemy.engine import make_url

from packages.core.models import Project
from packages.core.schemas_studio import StudioPreview
from packages.github.client import GitHubClient, TransientGitHubError
from packages.github.code_signals import Classification
from tests.conftest import TEST_DATABASE_URL

ROOT = Path(__file__).resolve().parent.parent

GITHUB_TOKEN_SENTINEL = "ghp_SENTINEL_TOKEN_NEVER_PERSIST_9999"
RESUME_PROSE_SENTINEL = "UNIQUE_RESUME_PROSE_SENTINEL_ADA_LOVELACE_7777"
JD_PROSE_SENTINEL = "UNIQUE_JD_PROSE_SENTINEL_PLATFORM_8888"

RAW_CODE_A_SENTINEL = "RAW_CODE_SENTINEL_REPO_A_DO_NOT_PERSIST"
RAW_CODE_B_SENTINEL = "RAW_CODE_SENTINEL_REPO_B_DO_NOT_PERSIST"
RAW_CODE_C_SENTINEL = "RAW_CODE_SENTINEL_REPO_C_DO_NOT_PERSIST"

COMMIT_A = "1" * 40
COMMIT_B = "2" * 40
COMMIT_C = "3" * 40
COMMIT_D = "4" * 40
COMMIT_E = "5" * 40
COMMIT_PRIV = "6" * 40

LAMBDA_CODE = f"""# {RAW_CODE_A_SENTINEL}
def lambda_handler(event, context):
    message = event.get("message", "")
    return {{"statusCode": 200, "body": message}}
"""

FASTAPI_CODE = f"""# {RAW_CODE_B_SENTINEL}
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health():
    return {{"ok": True}}
"""

K8S_MANIFEST = f"""# {RAW_CODE_C_SENTINEL}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: worker
spec:
  template:
    spec:
      containers:
        - name: worker
          image: ghcr.io/owner/worker:1.0
"""

DOCKERFILE_CODE = f"""# {RAW_CODE_C_SENTINEL}
FROM python:3.12
COPY . /app
"""

JOB_DESCRIPTION = (
    f"About this role:\n- {JD_PROSE_SENTINEL}\n"
    "Responsibilities:\n- Build backend APIs and container workloads.\n"
    "Requirements:\n- AWS Lambda, Python, Docker, and Kubernetes\n- 3 years of experience\n"
    "Preferred qualifications:\n- FastAPI and CI/CD\n- Bachelor's degree\n"
)

RESUME_BYTES = f"""Ada Lovelace
ada.sentinel@example.com | +1 (555) 555-0199

Summary
Backend systems engineer.

Experience
Staff Engineer, Analytical Engines Ltd
• {RESUME_PROSE_SENTINEL}

Skills
Python, PostgreSQL
""".encode()


@dataclass
class RepoFixture:
    external_id: str
    name: str
    full_name: str
    commit_sha: str
    files: dict[str, str]
    language: str = "Python"
    dependency_technologies: list[str] = field(default_factory=list)
    description: str = "Fixture repository."
    is_private: bool = False
    is_fork: bool = False
    is_archived: bool = False
    include: bool | None = None
    pinned: bool = False

    def api_payload(self) -> dict[str, Any]:
        return {
            "id": int(self.external_id),
            "name": self.name,
            "full_name": self.full_name,
            "html_url": f"https://github.com/{self.full_name}",
            "description": self.description,
            "language": self.language,
            "default_branch": "main",
            "private": self.is_private,
            "fork": self.is_fork,
            "archived": self.is_archived,
        }


class FakeGitHubServer:
    """Loopback GitHub REST stub that records check attempts across processes."""

    def __init__(
        self,
        repos: list[RepoFixture],
        *,
        fail_first: dict[str, tuple[int, int]] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self.repos = {repo.full_name: repo for repo in repos}
        # Map full_name -> (remaining transient failures, HTTP status)
        self._fail_remaining: dict[str, list[int]] = {
            name: [count, status] for name, (count, status) in (fail_first or {}).items()
        }
        self.all_repo_checks: list[str] = []
        self.phase_repo_checks: list[str] = []
        self.phase_paths: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def api_root(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def client(self, token: str | None = GITHUB_TOKEN_SENTINEL) -> GitHubClient:
        return GitHubClient(token=token, api_root=self.api_root, timeout=5.0)

    def start_phase(self) -> None:
        with self._lock:
            self.phase_repo_checks.clear()
            self.phase_paths.clear()

    def counts(self) -> dict[str, int]:
        with self._lock:
            return dict(Counter(self.all_repo_checks))

    def snapshot_phase(self) -> tuple[list[str], list[str]]:
        with self._lock:
            return list(self.phase_repo_checks), list(self.phase_paths)

    def start(self) -> FakeGitHubServer:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def _send_json(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                with parent._lock:
                    parent.phase_paths.append(path)

                if path == "/search/code":
                    q = (query.get("q") or [""])[0]
                    repo_name = q.split("repo:", 1)[-1].strip() if "repo:" in q else ""
                    self._send_json(
                        200,
                        {
                            "items": [
                                {"path": "stale.py", "repository": {"full_name": repo_name}},
                            ]
                        },
                    )
                    return

                parts = [p for p in path.split("/") if p]
                if len(parts) < 3 or parts[0] != "repos":
                    self._send_json(404, {"message": "not found"})
                    return
                full_name = f"{parts[1]}/{parts[2]}"
                repo = parent.repos.get(full_name)
                if repo is None:
                    self._send_json(404, {"message": "not found"})
                    return

                rest = parts[3:]
                if not rest:
                    with parent._lock:
                        parent.all_repo_checks.append(full_name)
                        parent.phase_repo_checks.append(full_name)
                        failure = parent._fail_remaining.get(full_name)
                        if failure and failure[0] > 0:
                            failure[0] -= 1
                            fail_status = failure[1]
                        else:
                            fail_status = None
                    if fail_status is not None:
                        self._send_json(fail_status, {"message": "transient upstream error"})
                        return
                    self._send_json(200, repo.api_payload())
                    return

                tree_sha = hashlib.sha1(f"tree:{full_name}:{repo.commit_sha}".encode()).hexdigest()
                if rest == ["commits", "main"]:
                    self._send_json(
                        200,
                        {"sha": repo.commit_sha, "commit": {"tree": {"sha": tree_sha}}},
                    )
                    return

                blobs: dict[str, tuple[str, bytes]] = {}
                for filename, text_body in repo.files.items():
                    raw = text_body.encode("utf-8")
                    sha = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
                    blobs[filename] = (sha, raw)

                if rest == ["git", "trees", tree_sha]:
                    self._send_json(
                        200,
                        {
                            "sha": tree_sha,
                            "tree": [
                                {
                                    "path": filename,
                                    "sha": sha,
                                    "size": len(raw),
                                    "type": "blob",
                                    "mode": "100644",
                                }
                                for filename, (sha, raw) in blobs.items()
                            ],
                        },
                    )
                    return

                if len(rest) == 3 and rest[:2] == ["git", "blobs"]:
                    wanted_sha = rest[2]
                    match = next((raw for sha, raw in blobs.values() if sha == wanted_sha), None)
                    if match is None:
                        self._send_json(404, {"message": "blob not found"})
                        return
                    self._send_json(
                        200,
                        {
                            "sha": wanted_sha,
                            "size": len(match),
                            "encoding": "base64",
                            "content": base64.b64encode(match).decode("ascii"),
                        },
                    )
                    return

                self._send_json(404, {"message": "not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


@pytest.fixture
def fake_github_factory() -> Iterator[Any]:
    servers: list[FakeGitHubServer] = []

    def _make(
        repos: list[RepoFixture], *, fail_first: dict[str, tuple[int, int]] | None = None
    ) -> FakeGitHubServer:
        server = FakeGitHubServer(repos, fail_first=fail_first).start()
        servers.append(server)
        return server

    try:
        yield _make
    finally:
        for server in servers:
            server.stop()


def _three_eligible_repos() -> list[RepoFixture]:
    """Three deterministic eligible repos ordered A -> B -> C by search_code ranking."""
    return [
        RepoFixture(
            external_id="101",
            name="repo-a",
            full_name="owner/repo-a",
            commit_sha=COMMIT_A,
            dependency_technologies=["FastAPI", "Docker", "Kubernetes"],
            pinned=True,
            files={"src/handler.py": LAMBDA_CODE},
        ),
        RepoFixture(
            external_id="102",
            name="repo-b",
            full_name="owner/repo-b",
            commit_sha=COMMIT_B,
            dependency_technologies=["FastAPI", "Docker"],
            files={
                "api/routes.py": FASTAPI_CODE,
                "requirements.txt": "fastapi>=0.115\n",
                "README.md": "Docker is used for local development.\n",
            },
        ),
        RepoFixture(
            external_id="103",
            name="repo-c",
            full_name="owner/repo-c",
            commit_sha=COMMIT_C,
            dependency_technologies=["Kubernetes"],
            files={
                "k8s/deploy.yml": K8S_MANIFEST,
                "Dockerfile": DOCKERFILE_CODE,
            },
        ),
    ]


async def _seed_candidate_and_repos(
    client: Any,
    committing_sessionmaker: Any,
    repos: list[RepoFixture],
) -> tuple[str, str, dict[str, uuid.UUID]]:
    suffix = uuid.uuid4().hex[:8]
    cand = await client.post(
        "/candidates",
        json={"name": "Ada Lovelace", "email": f"ada-{suffix}@example.com"},
    )
    assert cand.status_code == 201, cand.text
    candidate_id = cand.json()["id"]

    prof = await client.post(
        "/profiles",
        json={
            "candidate_id": candidate_id,
            "label": "backend",
            "phone": "+1-555-0199",
            "location": "Austin, TX",
            "work_auth": "US citizen",
            "needs_sponsorship": False,
        },
    )
    assert prof.status_code == 201, prof.text
    profile_id = prof.json()["id"]

    uploaded = await client.post(
        "/resumes",
        data={"candidate_id": candidate_id},
        files={"file": ("resume.txt", io.BytesIO(RESUME_BYTES), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    linked = await client.post(
        f"/resumes/{uploaded.json()['id']}/set-base", params={"profile_id": profile_id}
    )
    assert linked.status_code == 200, linked.text

    ids: dict[str, uuid.UUID] = {}
    async with committing_sessionmaker() as session:
        for repo in repos:
            row = Project(
                candidate_id=uuid.UUID(candidate_id),
                source="github",
                external_id=repo.external_id,
                name=repo.name,
                full_name=repo.full_name,
                url=f"https://github.com/{repo.full_name}",
                description=repo.description,
                language=repo.language,
                topics_json=[],
                dependency_technologies=list(repo.dependency_technologies),
                is_private=repo.is_private,
                is_fork=repo.is_fork,
                is_archived=repo.is_archived,
                include=repo.include,
                pinned=repo.pinned,
            )
            session.add(row)
            await session.flush()
            ids[repo.full_name] = row.id
        await session.commit()
    return candidate_id, profile_id, ids


@pytest_asyncio.fixture
async def clean_pilot_schema(engine: Any) -> AsyncIterator[str]:
    from packages.tailor.studio_pilot import PILOT_CHECKPOINT_SCHEMA

    async with engine.begin() as conn:
        await conn.execute(text(f"DROP SCHEMA IF EXISTS {PILOT_CHECKPOINT_SCHEMA} CASCADE"))
    try:
        yield PILOT_CHECKPOINT_SCHEMA
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {PILOT_CHECKPOINT_SCHEMA} CASCADE"))


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = TEST_DATABASE_URL
    env["TEST_DATABASE_URL"] = TEST_DATABASE_URL
    env["GITHUB_TOKEN"] = GITHUB_TOKEN_SENTINEL
    env["LLM_PROVIDER"] = "stub"
    env["PYTHONPATH"] = str(ROOT)
    # Keep LangSmith strictly disabled in child processes.
    env["LANGCHAIN_TRACING_V2"] = "false"
    env["LANGSMITH_TRACING"] = "false"
    return env


def _read_signal_line(stream: Any, *, timeout_s: float = 15.0) -> dict[str, Any]:
    """Read lines until a checkpoint_committed JSON event arrives; never uses sleep."""
    fd = stream.fileno()
    deadline_chunks: list[str] = []
    while True:
        ready, _, _ = select.select([fd], [], [], timeout_s)
        if not ready:
            raise TimeoutError(
                "Timed out waiting for explicit checkpoint_committed signal. "
                f"Saw stderr: {''.join(deadline_chunks)!r}"
            )
        line = stream.readline()
        if not line:
            stderr_text = "".join(deadline_chunks)
            raise RuntimeError(
                f"Child exited before emitting checkpoint_committed. Stderr: {stderr_text!r}"
            )
        deadline_chunks.append(line)
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("event") == "checkpoint_committed":
            return payload


async def test_restart_across_processes_resumes_only_third_repository(
    client: Any,
    committing_sessionmaker: Any,
    fake_github_factory: Any,
    clean_pilot_schema: str,
    tmp_path: Path,
) -> None:
    """Process 1 checks A and B and dies after B's checkpoint commits; Process 2 checks only C."""
    from packages.tailor.studio_pilot import psycopg_database_url

    repos = _three_eligible_repos()
    candidate_id, profile_id, _ = await _seed_candidate_and_repos(
        client, committing_sessionmaker, repos
    )
    server = fake_github_factory(repos)
    jd_path = tmp_path / "posting.txt"
    jd_path.write_text(JOB_DESCRIPTION, encoding="utf-8")
    run_id = f"pilot-restart-{uuid.uuid4().hex}"

    # Process 1: pause right after B's checkpoint is durably committed, then SIGKILL.
    server.start_phase()
    proc1 = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "scripts.studio_evidence_pilot",
            "--run-id",
            run_id,
            "--candidate-id",
            candidate_id,
            "--profile-id",
            profile_id,
            "--job-description-file",
            str(jd_path),
            "--github-api-root",
            server.api_root,
            "--pause-after-checks",
            "2",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_env(),
        cwd=str(ROOT),
    )
    try:
        signal_event = _read_signal_line(proc1.stderr)
        assert signal_event["completed_checks"] == 2
        assert signal_event["run_id"] == run_id

        # Verify on a fresh Postgres connection that B's checkpoint is durably committed.
        async with (
            await psycopg.AsyncConnection.connect(
                psycopg_database_url(TEST_DATABASE_URL), row_factory=dict_row
            ) as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(
                f"SELECT count(*) AS n FROM {clean_pilot_schema}.checkpoints WHERE thread_id = %s",
                (run_id,),
            )
            row = await cur.fetchone()
            assert row is not None and row["n"] >= 3

        # Hard-kill Process 1 before C starts (real process crash, no cleanup).
        proc1.kill()
        proc1.wait(timeout=10)
    finally:
        if proc1.poll() is None:
            proc1.kill()
            proc1.wait(timeout=10)

    first_run_checks, first_run_paths = server.snapshot_phase()
    assert first_run_checks == ["owner/repo-a", "owner/repo-b"]
    assert not any("owner/repo-c" in path for path in first_run_paths)

    # Process 2: fresh Python process, fresh DB connections, same run ID.
    server.start_phase()
    proc2 = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.studio_evidence_pilot",
            "--run-id",
            run_id,
            "--candidate-id",
            candidate_id,
            "--profile-id",
            profile_id,
            "--job-description-file",
            str(jd_path),
            "--github-api-root",
            server.api_root,
        ],
        capture_output=True,
        text=True,
        env=_child_env(),
        cwd=str(ROOT),
        timeout=30,
        check=False,
    )
    assert proc2.returncode == 0, f"Process 2 failed:\nstdout={proc2.stdout}\nstderr={proc2.stderr}"

    after_restart_checks, after_restart_paths = server.snapshot_phase()
    assert after_restart_checks == ["owner/repo-c"]
    assert not any("owner/repo-a" in path or "owner/repo-b" in path for path in after_restart_paths)
    assert server.counts() == {"owner/repo-a": 1, "owner/repo-b": 1, "owner/repo-c": 1}

    # Confirm repository selection wrote `selected_project_ids` once across both processes.
    async with (
        await psycopg.AsyncConnection.connect(
            psycopg_database_url(TEST_DATABASE_URL), row_factory=dict_row
        ) as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(
            f"SELECT checkpoint -> 'channel_versions' ->> 'selected_project_ids' AS sel_ver "
            f"FROM {clean_pilot_schema}.checkpoints "
            "WHERE thread_id = %s AND checkpoint -> 'channel_versions' ? 'selected_project_ids'",
            (run_id,),
        )
        ver_rows = await cur.fetchall()
    assert len(ver_rows) >= 5
    assert len({row["sel_ver"] for row in ver_rows}) == 1

    preview = StudioPreview.model_validate_json(proc2.stdout)
    assert preview.code_review is not None
    assert preview.code_review.repositories_checked == 3
    assert preview.project_evidence == "Live code evidence checked 3 GitHub repositories."
    by_repo = {finding.full_name for finding in preview.code_review.findings}
    assert by_repo == {"owner/repo-a", "owner/repo-b", "owner/repo-c"}
    by_commit = {finding.commit_sha for finding in preview.code_review.findings}
    assert by_commit == {COMMIT_A, COMMIT_B, COMMIT_C}
    classifications = {finding.classification for finding in preview.code_review.findings}
    assert classifications == {
        Classification.IMPLEMENTED,
        Classification.DECLARED,
        Classification.DOCUMENTED,
    }
    needs = {item.skill: item for item in preview.code_review.requirements}
    assert needs["aws_lambda"].status == "implementation found · contribution unconfirmed"
    assert needs["fastapi"].status == "implementation found · contribution unconfirmed"
    assert needs["kubernetes"].status == "implementation found · contribution unconfirmed"
    assert needs["docker"].status == "implementation found · contribution unconfirmed"


async def test_transient_failure_on_third_repo_retries_only_c(
    client: Any,
    committing_sessionmaker: Any,
    fake_github_factory: Any,
    clean_pilot_schema: str,
) -> None:
    """Transient 503 on C fails before C's checkpoint; recovery retries C with A and B at 1."""
    from packages.tailor.studio_pilot import run_studio_evidence_pilot

    repos = _three_eligible_repos()
    candidate_id, profile_id, _ = await _seed_candidate_and_repos(
        client, committing_sessionmaker, repos
    )
    server = fake_github_factory(repos, fail_first={"owner/repo-c": (1, 503)})
    run_id = f"pilot-retry-{uuid.uuid4().hex}"

    server.start_phase()
    with pytest.raises(TransientGitHubError, match="503"):
        await run_studio_evidence_pilot(
            committing_sessionmaker,
            run_id=run_id,
            candidate_id=uuid.UUID(candidate_id),
            profile_id=uuid.UUID(profile_id),
            job_description=JOB_DESCRIPTION,
            database_url=TEST_DATABASE_URL,
            github=server.client(),
        )

    attempt1_checks, _ = server.snapshot_phase()
    assert attempt1_checks == ["owner/repo-a", "owner/repo-b", "owner/repo-c"]
    assert server.counts() == {"owner/repo-a": 1, "owner/repo-b": 1, "owner/repo-c": 1}

    # Fresh runner + fresh DB session resuming the same run ID retries only C.
    server.start_phase()
    preview = await run_studio_evidence_pilot(
        committing_sessionmaker,
        run_id=run_id,
        candidate_id=uuid.UUID(candidate_id),
        profile_id=uuid.UUID(profile_id),
        job_description=JOB_DESCRIPTION,
        database_url=TEST_DATABASE_URL,
        github=server.client(),
    )

    attempt2_checks, attempt2_paths = server.snapshot_phase()
    assert attempt2_checks == ["owner/repo-c"]
    assert not any("owner/repo-a" in path or "owner/repo-b" in path for path in attempt2_paths)
    assert server.counts() == {"owner/repo-a": 1, "owner/repo-b": 1, "owner/repo-c": 2}
    assert preview.code_review is not None
    assert preview.code_review.repositories_checked == 3
    assert {f.full_name for f in preview.code_review.findings} == {
        "owner/repo-a",
        "owner/repo-b",
        "owner/repo-c",
    }


async def test_parity_with_existing_live_code_preview_and_save_authority(
    client: Any,
    committing_sessionmaker: Any,
    fake_github_factory: Any,
    clean_pilot_schema: str,
    monkeypatch: Any,
) -> None:
    """Pilot preview matches existing live_code preview across eligibility, limits, and caveats."""
    from packages.github.code_search import search_code
    from packages.tailor.code_evidence import extract_code_requirements
    from packages.tailor.studio_pilot import run_studio_evidence_pilot

    conflicting_repo = RepoFixture(
        external_id="104",
        name="repo-d-conflict",
        full_name="owner/repo-d-conflict",
        commit_sha=COMMIT_D,
        dependency_technologies=["Docker"],
        files={
            "src/handler.py": LAMBDA_CODE,
            "README.md": "AWS Lambda is planned and not supported.\n",
        },
    )
    fifth_repo = RepoFixture(
        external_id="105",
        name="repo-e-over-cap",
        full_name="owner/repo-e-over-cap",
        commit_sha=COMMIT_E,
        dependency_technologies=[],
        files={"src/handler.py": LAMBDA_CODE},
    )
    private_repo = RepoFixture(
        external_id="106",
        name="repo-private",
        full_name="owner/repo-private",
        commit_sha=COMMIT_PRIV,
        dependency_technologies=["FastAPI", "Kubernetes"],
        is_private=True,
        include=True,
        files={"api/routes.py": FASTAPI_CODE},
    )
    fork_repo = RepoFixture(
        external_id="107",
        name="repo-fork",
        full_name="owner/repo-fork",
        commit_sha="7" * 40,
        dependency_technologies=["FastAPI", "Docker", "Kubernetes"],
        is_fork=True,
        files={"src/handler.py": LAMBDA_CODE},
    )
    archived_repo = RepoFixture(
        external_id="108",
        name="repo-archived",
        full_name="owner/repo-archived",
        commit_sha="8" * 40,
        dependency_technologies=["FastAPI", "Docker", "Kubernetes"],
        is_archived=True,
        files={"src/handler.py": LAMBDA_CODE},
    )
    excluded_repo = RepoFixture(
        external_id="109",
        name="repo-excluded",
        full_name="owner/repo-excluded",
        commit_sha="9" * 40,
        dependency_technologies=["FastAPI", "Docker", "Kubernetes"],
        include=False,
        files={"src/handler.py": LAMBDA_CODE},
    )

    repos = [
        *_three_eligible_repos(),
        conflicting_repo,
        fifth_repo,
        private_repo,
        fork_repo,
        archived_repo,
        excluded_repo,
    ]
    candidate_id, profile_id, _ = await _seed_candidate_and_repos(
        client, committing_sessionmaker, repos
    )
    server = fake_github_factory(repos)

    # Compare both authenticated (private repo eligible) and unauthenticated (private repo gated).
    for label, token in [("with-token", GITHUB_TOKEN_SENTINEL), ("anonymous", None)]:

        async def _existing_retrieve(
            text_jd: str,
            projects: Any,
            *,
            _token: str | None = token,
        ) -> Any:
            return await search_code(
                projects, extract_code_requirements(text_jd), github=server.client(token=_token)
            )

        monkeypatch.setattr("apps.api.routers.resume_studio.retrieve_evidence", _existing_retrieve)
        existing_resp = await client.post(
            "/resumes/synthesize",
            json={
                "candidate_id": candidate_id,
                "profile_id": profile_id,
                "job_description": JOB_DESCRIPTION,
                "live_code": True,
                "use_model": False,
            },
        )
        assert existing_resp.status_code == 200, existing_resp.text
        existing = StudioPreview.model_validate(existing_resp.json())

        pilot = await run_studio_evidence_pilot(
            committing_sessionmaker,
            run_id=f"pilot-parity-{label}-{uuid.uuid4().hex}",
            candidate_id=uuid.UUID(candidate_id),
            profile_id=uuid.UUID(profile_id),
            job_description=JOB_DESCRIPTION,
            database_url=TEST_DATABASE_URL,
            github=server.client(token=token),
        )

        assert pilot == existing
        assert pilot.code_review is not None
        assert pilot.code_review.repositories_checked == 4
        assert "Repository coverage was limited to the top 4 matches." in (
            pilot.code_review.limitations
        )
        checked_repos = {f.full_name for f in pilot.code_review.findings}
        ineligible = {
            "owner/repo-fork",
            "owner/repo-archived",
            "owner/repo-excluded",
            "owner/repo-e-over-cap",
        }
        assert not (checked_repos & ineligible)

        if token is None:
            assert "Private repositories require the configured GitHub token." in (
                pilot.code_review.limitations
            )
            assert "owner/repo-private" not in checked_repos
            # repo-d-conflict is in top 4 when private repo is excluded: verify caveat suppression.
            conflict_findings = [
                f for f in pilot.code_review.findings if f.full_name == "owner/repo-d-conflict"
            ]
            assert any(f.classification is Classification.INSUFFICIENT for f in conflict_findings)
            lambda_conflict = next(
                f
                for f in conflict_findings
                if "aws_lambda" in f.skills and f.classification is Classification.IMPLEMENTED
            )
            assert lambda_conflict.can_support_claim is False
            assert lambda_conflict.wording == ()
            assert "conflicting" in lambda_conflict.verification_status
        else:
            private_findings = [
                f for f in pilot.code_review.findings if f.full_name == "owner/repo-private"
            ]
            assert private_findings
            assert all(f.is_private and f.permalink is None for f in private_findings)

    # Existing save route remains authoritative for owner approval and commit revalidation.
    async def _auth_retrieve(text_jd: str, projects: Any) -> Any:
        return await search_code(
            projects, extract_code_requirements(text_jd), github=server.client()
        )

    monkeypatch.setattr("apps.api.routers.resume_studio.retrieve_evidence", _auth_retrieve)
    pilot_preview = await run_studio_evidence_pilot(
        committing_sessionmaker,
        run_id=f"pilot-save-check-{uuid.uuid4().hex}",
        candidate_id=uuid.UUID(candidate_id),
        profile_id=uuid.UUID(profile_id),
        job_description=JOB_DESCRIPTION,
        database_url=TEST_DATABASE_URL,
        github=server.client(),
    )
    assert pilot_preview.code_review is not None and pilot_preview.code_review.suggestions
    suggestion = pilot_preview.code_review.suggestions[0]
    chosen_ids = [str(p.id) for p in pilot_preview.projects if p.chosen]

    # Mutate repo-a's commit SHA after preview: existing save route must refuse stale approval.
    server.repos["owner/repo-a"].commit_sha = "f" * 40
    stale = await client.post(
        "/resumes/synthesize/save",
        json={
            "candidate_id": candidate_id,
            "profile_id": profile_id,
            "base_resume_id": str(pilot_preview.base_resume_id),
            "selected_project_ids": chosen_ids,
            "job_description": JOB_DESCRIPTION,
            "live_code": True,
            "contact": pilot_preview.contact.model_dump(),
            "sections": pilot_preview.sections,
            "code_approvals": [
                {
                    "finding_id": suggestion.finding_id,
                    "entry": suggestion.entry,
                    "wording": suggestion.wording[0],
                    "contribution_confirmed": True,
                }
            ],
        },
    )
    assert stale.status_code == 400 and "new preview" in stale.text


async def test_persisted_checkpoints_and_writes_keep_secrets_code_and_resume_out(
    client: Any,
    committing_sessionmaker: Any,
    fake_github_factory: Any,
    clean_pilot_schema: str,
) -> None:
    """Inspect checkpoints, blobs, and pending writes (raw and decoded) for accidental leakage."""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from packages.tailor.studio_pilot import psycopg_database_url, run_studio_evidence_pilot

    repos = [
        *_three_eligible_repos(),
        RepoFixture(
            external_id="106",
            name="repo-private",
            full_name="owner/repo-private",
            commit_sha=COMMIT_PRIV,
            dependency_technologies=["FastAPI"],
            is_private=True,
            include=True,
            files={"api/routes.py": FASTAPI_CODE},
        ),
    ]
    candidate_id, profile_id, _ = await _seed_candidate_and_repos(
        client, committing_sessionmaker, repos
    )
    # Trigger one transient failure on C so checkpoint_writes records a pending error write too.
    server = fake_github_factory(repos, fail_first={"owner/repo-c": (1, 503)})
    run_id = f"pilot-privacy-{uuid.uuid4().hex}"

    with pytest.raises(TransientGitHubError):
        await run_studio_evidence_pilot(
            committing_sessionmaker,
            run_id=run_id,
            candidate_id=uuid.UUID(candidate_id),
            profile_id=uuid.UUID(profile_id),
            job_description=JOB_DESCRIPTION,
            database_url=TEST_DATABASE_URL,
            github=server.client(token=GITHUB_TOKEN_SENTINEL),
        )

    preview = await run_studio_evidence_pilot(
        committing_sessionmaker,
        run_id=run_id,
        candidate_id=uuid.UUID(candidate_id),
        profile_id=uuid.UUID(profile_id),
        job_description=JOB_DESCRIPTION,
        database_url=TEST_DATABASE_URL,
        github=server.client(token=GITHUB_TOKEN_SENTINEL),
    )
    assert preview.code_review is not None and preview.code_review.repositories_checked == 4

    serde = JsonPlusSerializer()
    parsed_db = make_url(TEST_DATABASE_URL)
    forbidden = [
        GITHUB_TOKEN_SENTINEL,
        TEST_DATABASE_URL,
        *([] if not parsed_db.password else [f":{parsed_db.password}@"]),
        RESUME_PROSE_SENTINEL,
        "ada.sentinel@example.com",
        "555-0199",
        "Analytical Engines Ltd",
        JD_PROSE_SENTINEL,
        RAW_CODE_A_SENTINEL,
        RAW_CODE_B_SENTINEL,
        RAW_CODE_C_SENTINEL,
        "def lambda_handler",
        "@app.get",
        "FROM python:3.12",
        "ghcr.io/owner/worker:1.0",
        "https://github.com/owner/repo-private/blob/",
        "<section",
    ]

    persisted_texts: list[str] = []
    async with (
        await psycopg.AsyncConnection.connect(
            psycopg_database_url(TEST_DATABASE_URL), row_factory=dict_row
        ) as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(
            f"SELECT checkpoint::text AS cp, metadata::text AS meta "
            f"FROM {clean_pilot_schema}.checkpoints WHERE thread_id = %s",
            (run_id,),
        )
        checkpoints = await cur.fetchall()
        assert len(checkpoints) >= 5
        for row in checkpoints:
            persisted_texts.append(row["cp"])
            persisted_texts.append(row["meta"])

        await cur.execute(
            f"SELECT channel, type, blob FROM {clean_pilot_schema}.checkpoint_blobs "
            "WHERE thread_id = %s",
            (run_id,),
        )
        blobs = await cur.fetchall()
        assert blobs
        for row in blobs:
            raw_blob = bytes(row["blob"]) if row["blob"] is not None else b""
            persisted_texts.append(raw_blob.decode("utf-8", errors="ignore"))
            if row["type"] != "empty" and row["blob"] is not None:
                decoded = serde.loads_typed((row["type"], raw_blob))
                persisted_texts.append(repr(decoded))

        await cur.execute(
            f"SELECT channel, type, blob FROM {clean_pilot_schema}.checkpoint_writes "
            "WHERE thread_id = %s",
            (run_id,),
        )
        writes = await cur.fetchall()
        assert writes
        for row in writes:
            raw_blob = bytes(row["blob"]) if row["blob"] is not None else b""
            persisted_texts.append(raw_blob.decode("utf-8", errors="ignore"))
            if row["type"] and row["type"] != "empty" and row["blob"] is not None:
                decoded = serde.loads_typed((row["type"], raw_blob))
                persisted_texts.append(repr(decoded))

    corpus = "\n".join(persisted_texts)
    for sentinel in forbidden:
        assert sentinel not in corpus, f"Forbidden value leaked into checkpoints: {sentinel!r}"
