"""MCP server — Gate 4.

The tools are driven end to end here: MCP tool → HTTP client → the real
FastAPI app → Postgres. Only the socket is bypassed (ASGI transport), so the
completeness gate, the state machine, and the error envelope are all the real
ones.

Gate 4 asks that Claude Code can drive a full apply-to-review cycle
conversationally. `test_full_cycle_through_tools_only` is that cycle, using
nothing but tool calls.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import ASGITransport

from apps.mcp import server as mcp_server
from apps.mcp.client import ApiUnavailable, JobrunnerClient
from apps.worker import run as worker_run

APPLY_URL = "https://boards.greenhouse.io/acme/jobs/4012345"


@pytest.fixture(autouse=True)
def _bind_tools_to_app(client, monkeypatch):
    """Point the MCP client at the ASGI app the test client already drives."""
    from apps.api.main import app

    bound = JobrunnerClient(
        base_url="http://test", transport=ASGITransport(app=app, client=("127.0.0.1", 1))
    )
    monkeypatch.setattr(mcp_server, "_client", bound)
    return bound


async def call(name: str, **arguments: Any) -> Any:
    """Invoke a tool the way an MCP client would, and unwrap the payload."""
    result = await mcp_server.server.call_tool(name, arguments)
    assert not result.is_error, result.content
    if result.structured_content and "result" in result.structured_content:
        return result.structured_content["result"]
    return json.loads(result.content[0].text)


# --------------------------------------------------------------------------
# Tool surface
# --------------------------------------------------------------------------


async def test_every_tool_is_documented() -> None:
    """A tool with no description is unusable by a model."""
    tools = await mcp_server.server.list_tools()
    assert len(tools) >= 15
    for tool in tools:
        assert tool.description and len(tool.description.strip()) > 20, tool.name
        assert tool.input_schema["type"] == "object"


async def test_no_tool_submits_an_application() -> None:
    """§2.3 — the tool surface must not offer a way around the approval gate."""
    names = {t.name for t in await mcp_server.server.list_tools()}
    assert not any("submit" in n for n in names if n != "submit_otp")


async def test_no_tool_tailors_on_its_own() -> None:
    """There is no standalone `tailor_resume`, and the reason has changed.

    It used to be that tailoring did not exist. It does now — the apply
    pipeline calls it on every run. What is still absent is a tool that tailors
    *without* applying, because the document it produced would belong to no
    application: nothing would upload it, and §9's `tailored_resume_id` would
    stay null while a résumé sat in storage looking finished.

    `compare_tailoring` is not that tool. It tailors against a real parked
    application and attaches the result to it, which is what makes the output
    something the owner can actually send.
    """
    names = {t.name for t in await mcp_server.server.list_tools()}
    assert "tailor_resume" not in names
    assert "preview_resume" in names
    assert "compare_tailoring" in names


async def test_choosing_a_tailoring_is_not_approving_one() -> None:
    """§2.3 — picking which résumé goes is upstream of the approval gate.

    Two separate tools on purpose. A single "choose and send" would collapse a
    decision about the *document* into a decision about *submitting*, and the
    approval gate is the one thing that must stay its own deliberate act.
    """
    tools = {t.name: t for t in await mcp_server.server.list_tools()}
    assert "select_tailoring" in tools
    description = (tools["select_tailoring"].description or "").lower()
    assert "not approving" in description or "stays parked" in description


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


async def test_detect_ats() -> None:
    result = await call("detect_ats", url=APPLY_URL)
    assert result["ats"] == "greenhouse"
    assert result["supported"] is True


async def test_detect_unknown_site() -> None:
    result = await call("detect_ats", url="https://acme.com/careers/1")
    assert result["ats"] is None


async def test_supported_ats() -> None:
    assert "greenhouse" in (await call("supported_ats"))["supported"]


async def test_search_postings_explains_an_empty_index() -> None:
    """An empty list must not read as 'no matches'."""
    result = await call("search_postings", query="engineer")
    assert result["results"] == []
    assert result["total_indexed"] == 0
    assert "Phase 5" in result["note"]


# --------------------------------------------------------------------------
# Applying and the approval gate
# --------------------------------------------------------------------------


async def test_apply_to_url_queues(complete_candidate) -> None:
    result = await call("apply_to_url", **complete_candidate, url=APPLY_URL)
    assert result["status"] == "queued"


async def test_apply_surfaces_an_incomplete_profile(bare_candidate) -> None:
    """The completeness gate reaches the tool caller, not just HTTP."""
    result = await call("apply_to_url", **bare_candidate, url=APPLY_URL)
    assert result["code"] == "invalid_request"
    assert "base_resume_id" in result["error"]


async def test_duplicate_application_surfaces(complete_candidate) -> None:
    await call("apply_to_url", **complete_candidate, url=APPLY_URL)
    result = await call("apply_to_url", **complete_candidate, url=APPLY_URL)
    assert result["code"] == "duplicate_application"


async def test_application_status_and_history(complete_candidate) -> None:
    created = await call("apply_to_url", **complete_candidate, url=APPLY_URL)

    status = await call("application_status", application_id=created["id"])
    assert status["status"] == "queued"

    history = await call("application_history", application_id=created["id"])
    assert [e["type"] for e in history["events"]] == ["created"]


async def test_list_applications_filters(complete_candidate) -> None:
    await call("apply_to_url", **complete_candidate, url=APPLY_URL)

    everything = await call("list_applications")
    queued = await call("list_applications", status="queued")
    submitted = await call("list_applications", status="submitted")

    assert everything["count"] == 1
    assert queued["count"] == 1
    assert submitted["count"] == 0


async def test_review_queue_is_empty_before_the_worker_runs(complete_candidate) -> None:
    await call("apply_to_url", **complete_candidate, url=APPLY_URL)
    assert (await call("review_queue"))["count"] == 0


# --------------------------------------------------------------------------
# Projects and résumés
# --------------------------------------------------------------------------


async def test_list_candidates_and_profiles(complete_candidate) -> None:
    assert len((await call("list_candidates"))["candidates"]) >= 1
    assert len((await call("list_profiles"))["profiles"]) >= 1


async def test_inspect_resume(complete_candidate) -> None:
    resumes = await call("list_resumes", candidate_id=complete_candidate["candidate_id"])
    parsed = await call("inspect_resume", resume_id=resumes["resumes"][0]["id"])

    assert parsed["contact"]["email"] == "ada@example.com"
    assert "experience" in parsed["sections"]


async def test_preview_resume_reports_sections(complete_candidate) -> None:
    resumes = await call("list_resumes", candidate_id=complete_candidate["candidate_id"])
    preview = await call(
        "preview_resume", resume_id=resumes["resumes"][0]["id"], job_text="Python backend"
    )

    assert "experience" in preview["sections"]
    assert preview["source_line_count"] > 0


async def test_curate_project_pins(complete_candidate, monkeypatch) -> None:
    import httpx

    from packages.github.client import GitHubClient

    repo = {
        "id": 1,
        "name": "jobrunner",
        "full_name": "octocat/jobrunner",
        "html_url": "https://github.com/octocat/jobrunner",
        "homepage": None,
        "description": "Local job-application agent",
        "language": "Python",
        "topics": [],
        "stargazers_count": 3,
        "forks_count": 0,
        "fork": False,
        "archived": False,
        "private": False,
        "pushed_at": "2026-08-01T10:00:00Z",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        return httpx.Response(200, json=[repo] if page == 1 else [])

    real_init = GitHubClient.__init__

    def patched(self, token=None, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, token, **kwargs)

    monkeypatch.setattr(GitHubClient, "__init__", patched)

    synced = await call(
        "sync_github_projects",
        candidate_id=complete_candidate["candidate_id"],
        username="octocat",
    )
    assert synced["added"] == 1

    listed = await call("list_projects", candidate_id=complete_candidate["candidate_id"])
    project_id = listed["projects"][0]["id"]

    curated = await call("curate_project", project_id=project_id, pinned=True)
    assert curated["pinned"] is True

    preview = await call("preview_projects", candidate_id=complete_candidate["candidate_id"])
    assert preview["selected"][0]["name"] == "jobrunner"
    assert "github.com/octocat/jobrunner" in preview["selected"][0]["rendered_link"]


# --------------------------------------------------------------------------
# Gate 4 — a full apply-to-review cycle, tools only
# --------------------------------------------------------------------------


async def test_full_cycle_through_tools_only(complete_candidate, monkeypatch, tmp_path) -> None:
    """Apply, work it, read the queue, answer the questions, approve."""
    import asyncio
    from contextlib import asynccontextmanager
    from pathlib import Path

    from apps.worker import browser as browser_mod

    fixture = (Path(__file__).parent / "fixtures" / "greenhouse_posting.html").read_text()

    @asynccontextmanager
    async def _page(ats: str, **kwargs):
        from apps.worker.browser import ephemeral_page

        async with ephemeral_page() as page:
            await page.route(
                "**/*",
                lambda route: asyncio.ensure_future(
                    route.fulfill(status=200, content_type="text/html", body=fixture)
                ),
            )
            yield page

    monkeypatch.setattr(browser_mod, "browser_page", _page)
    monkeypatch.setattr("apps.worker.apply_job.browser_page", _page)

    # 1. Check the site is supported.
    assert (await call("detect_ats", url=APPLY_URL))["supported"] is True

    # 2. Queue the application.
    created = await call("apply_to_url", **complete_candidate, url=APPLY_URL)
    application_id = created["id"]

    # 3. The worker fills the form and parks it.
    await worker_run.run_once(worker_id="mcp-test")

    # 4. Read what it is waiting on.
    queue = await call("review_queue")
    assert queue["count"] == 1
    parked = queue["waiting"][0]
    assert parked["application_id"] == application_id

    questions = {q["question"] for q in parked["unanswered"]}
    assert "Why do you want to work at Acme?" in questions
    # The résumé came from the profile, so it is not among the open questions.
    assert "resume" not in {q["key"] for q in parked["unanswered"]}

    # 5. Answer them in the employer's own keys and approve.
    answers = {
        q["key"]: ("1" if q["kind"] == "single_select" else "I admire the work.")
        for q in parked["unanswered"]
    }
    approved = await call("approve_application", application_id=application_id, answers=answers)
    assert approved["status"] == "running"

    # 6. The queue is clear.
    assert (await call("review_queue"))["count"] == 0

    history = await call("application_history", application_id=application_id)
    assert any(e["payload"].get("decision") == "approve" for e in history["events"])


async def test_rejecting_through_tools(complete_candidate, monkeypatch) -> None:
    import asyncio
    from contextlib import asynccontextmanager
    from pathlib import Path

    from apps.worker import browser as browser_mod

    fixture = (Path(__file__).parent / "fixtures" / "greenhouse_posting.html").read_text()

    @asynccontextmanager
    async def _page(ats: str, **kwargs):
        from apps.worker.browser import ephemeral_page

        async with ephemeral_page() as page:
            await page.route(
                "**/*",
                lambda route: asyncio.ensure_future(
                    route.fulfill(status=200, content_type="text/html", body=fixture)
                ),
            )
            yield page

    monkeypatch.setattr(browser_mod, "browser_page", _page)
    monkeypatch.setattr("apps.worker.apply_job.browser_page", _page)

    created = await call("apply_to_url", **complete_candidate, url=APPLY_URL)
    await worker_run.run_once(worker_id="mcp-test")

    rejected = await call("reject_application", application_id=created["id"], note="not a fit")

    assert rejected["status"] == "failed"
    assert rejected["failure_reason"] == "rejected_at_review"


# --------------------------------------------------------------------------
# Failure modes
# --------------------------------------------------------------------------


async def test_unreachable_api_is_reported_not_swallowed(monkeypatch) -> None:
    """A dead API should say so, not look like an empty result."""

    class _Dead(JobrunnerClient):
        async def request(self, *args: Any, **kwargs: Any) -> Any:
            raise ApiUnavailable(
                "Cannot reach the Jobrunner API at http://x. Start it with `make api`."
            )

    monkeypatch.setattr(mcp_server, "_client", _Dead())

    result = await call("review_queue")
    assert "Cannot reach" in result["error"]
    assert "make api" in result["error"]


async def test_not_found_surfaces_the_error_code() -> None:
    import uuid

    result = await call("application_status", application_id=str(uuid.uuid4()))
    assert result["code"] == "not_found"
