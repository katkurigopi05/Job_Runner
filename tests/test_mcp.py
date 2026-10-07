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


def _wire(tool: Any) -> dict[str, Any]:
    """The tool as its `tools/list` entry, which is what a client reads."""
    return tool.model_dump(by_alias=True, exclude_none=True)


async def test_the_owners_decisions_ask_the_owner_every_time() -> None:
    """§2.3 — over MCP the one approving is a model, so the client must ask.

    `approve_application` releases a real application and `submit_otp` resumes
    one. Their docstrings say to call them only when the owner has decided,
    and a docstring is a request. Claude Code shows a permission prompt on
    every call to a tool whose `tools/list` entry carries this flag, in every
    permission mode and past any allow rule; in the mode that never prompts it
    refuses the call.

    Asserted on the wire entry, with `is True`: Claude Code ignores any value
    that is not the JSON boolean.
    """
    tools = {t.name: _wire(t) for t in await mcp_server.server.list_tools()}
    asking = {
        name for name, tool in tools.items() if (tool.get("_meta") or {}).get(mcp_server.ASK_FLAG)
    }

    # The exact set, so adding or removing one is a deliberate act.
    assert asking == set(mcp_server.ASKS_THE_OWNER) == {"approve_application", "submit_otp"}
    for name in asking:
        assert tools[name]["_meta"][mcp_server.ASK_FLAG] is True, name


def test_the_project_settings_ask_too() -> None:
    """The same two tools, as a rule for a client that does not read the flag.

    `.claude/settings.json` is the project's shared Claude Code settings. An
    ask rule there prompts in every mode and wins over an allow rule. It names
    a tool as `mcp__<server>__<tool>`, so the server's name in `.mcp.json` is
    part of what has to agree: renaming the server would leave two rules that
    match nothing, and a rule that matches nothing asks nobody.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    servers = json.loads((root / ".mcp.json").read_text())["mcpServers"]
    assert list(servers) == [mcp_server.server.name]

    permissions = json.loads((root / ".claude" / "settings.json").read_text())["permissions"]
    expected = {f"mcp__{mcp_server.server.name}__{tool}" for tool in mcp_server.ASKS_THE_OWNER}
    assert set(permissions["ask"]) == expected
    assert not expected & set(permissions.get("allow", []))


# --------------------------------------------------------------------------
# The owner's own answers
# --------------------------------------------------------------------------
#
# `approve_application` took the missing answers, and `submit_otp` the code, as
# arguments of the call. The caller is a model, so a model typed what went on
# the form, and a question the pipeline could not map can be a
# work-authorization one. §2.2 says those are never model-written; the server's
# instructions asked for that, and an instruction is a request.
#
# Both values are now filled by asking the person: the server shows a form and
# the tool receives what was typed into it. They are not in either tool's input
# schema, so there is nothing for a model to fill in.


def _fills_in_every_field(schema: dict[str, Any]) -> dict[str, Any]:
    """An owner who answers everything: the first choice where there are choices."""
    return {
        name: (field["enum"][0] if "enum" in field else "I admire the work.")
        for name, field in schema["properties"].items()
    }


class _Owner:
    """The person at the keyboard, for a client that can show them a form."""

    def __init__(self, typed: Any = None, *, action: str = "accept", mode: str = "auto") -> None:
        self._typed, self._action, self._mode = typed, action, mode
        #: Every form the server showed, as its schema, and the message above it.
        self.shown: list[dict[str, Any]] = []
        self.messages: list[str] = []

    async def _answer(self, context: Any, params: Any) -> Any:
        from mcp import types

        asked = params.model_dump(by_alias=True, exclude_none=True)
        self.shown.append(asked["requestedSchema"])
        self.messages.append(asked["message"])
        if self._action != "accept":
            return types.ElicitResult(action=self._action)
        return types.ElicitResult(action="accept", content=self._typed(asked["requestedSchema"]))

    async def result(self, name: str, **arguments: Any) -> Any:
        from mcp.client import Client

        async with Client(
            mcp_server.server, elicitation_callback=self._answer, mode=self._mode
        ) as client:
            return await client.call_tool(name, arguments)

    async def calls(self, name: str, **arguments: Any) -> Any:
        result = await self.result(name, **arguments)
        assert not result.is_error, result.content
        if result.structured_content and "result" in result.structured_content:
            return result.structured_content["result"]
        return json.loads(result.content[0].text)


class _Api:
    """Stands in for the API: one parked application, and what was posted about it."""

    def __init__(self, unanswered: list[dict[str, Any]]) -> None:
        self.application = {
            "id": "app-1",
            "status": "needs_review",
            "review": {"unanswered": unanswered},
        }
        self.posted: list[tuple[str, Any]] = []

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if method == "GET":
            return self.application
        self.posted.append((path, kwargs.get("json")))
        return {"id": "app-1", "status": "running"}


WHY = {"key": "question_31", "question": "Why do you want to work at Acme?", "kind": "textarea"}
AUTHORIZED = {
    "key": "question_32",
    "question": "Are you legally authorized to work in the United States?",
    "kind": "single_select",
    "options": [{"label": "Yes", "value": "1"}, {"label": "No", "value": "0"}],
}

BOTH_PROTOCOLS = pytest.mark.parametrize("mode", ["legacy", "auto"])


@pytest.fixture
def parked(monkeypatch):
    def install(*unanswered: dict[str, Any]) -> _Api:
        api = _Api(list(unanswered))
        monkeypatch.setattr(mcp_server, "_client", api)
        return api

    return install


async def test_a_model_has_nowhere_to_put_an_answer_or_a_code() -> None:
    """Read from the wire entry: what a client is told it may pass."""
    tools = {t.name: _wire(t) for t in await mcp_server.server.list_tools()}

    assert set(tools["approve_application"]["inputSchema"]["properties"]) == {
        "application_id",
        "note",
    }
    assert set(tools["submit_otp"]["inputSchema"]["properties"]) == {"application_id"}


@BOTH_PROTOCOLS
async def test_open_questions_are_put_to_the_owner_in_the_employers_words(parked, mode) -> None:
    api = parked(WHY, AUTHORIZED)
    owner = _Owner(lambda schema: {"q1": "I admire the work.", "q2": "Yes"}, mode=mode)

    approved = await owner.calls("approve_application", application_id="app-1")

    assert approved["status"] == "running"
    [form] = owner.shown
    assert [field["title"] for field in form["properties"].values()] == [
        "Why do you want to work at Acme?",
        "Are you legally authorized to work in the United States?",
    ]
    # The employer's own choices, and no others: an answer off the menu is
    # refused at the form, long before it could be typed into a dropdown.
    assert form["properties"]["q2"]["enum"] == ["Yes", "No"]
    assert "enum" not in form["properties"]["q1"]
    # Posted under the employer's keys, as typed.
    assert api.posted == [
        (
            "/applications/app-1/review",
            {
                "approve": True,
                "answers": {"question_31": "I admire the work.", "question_32": "Yes"},
                "note": None,
            },
        )
    ]


@BOTH_PROTOCOLS
async def test_an_application_with_nothing_open_asks_nothing(parked, mode) -> None:
    api = parked()
    owner = _Owner(mode=mode)

    approved = await owner.calls("approve_application", application_id="app-1", note="go")

    assert approved["status"] == "running"
    assert owner.shown == []
    assert api.posted == [
        ("/applications/app-1/review", {"approve": True, "answers": {}, "note": "go"})
    ]


@BOTH_PROTOCOLS
@pytest.mark.parametrize("action", ["decline", "cancel"])
async def test_an_owner_who_closes_the_form_has_approved_nothing(parked, mode, action) -> None:
    """§2.3. Putting the form away is not a yes, and §2.4: nothing is guessed in its place."""
    api = parked(WHY)
    owner = _Owner(action=action, mode=mode)

    answered = await owner.calls("approve_application", application_id="app-1")

    assert api.posted == []
    assert answered["approved"] is False
    assert "/review" in answered["error"], "somewhere for the owner to go and answer"


@BOTH_PROTOCOLS
async def test_a_blank_answer_is_not_sent_as_an_answer(parked, mode) -> None:
    """§2.4: never leave blank. A field left empty stays an open question."""
    api = parked(WHY, AUTHORIZED)
    owner = _Owner(lambda schema: {"q1": "   ", "q2": "No"}, mode=mode)

    await owner.calls("approve_application", application_id="app-1")

    [(_, posted)] = api.posted
    assert posted["answers"] == {"question_32": "No"}


async def test_a_client_that_cannot_show_a_form_is_sent_to_the_dashboard(parked) -> None:
    """Another MCP client, or this one with forms off. The questions are not
    handed to the model to answer in their place."""
    from mcp.client import Client

    api = parked(WHY)

    async with Client(mcp_server.server) as client:
        result = await client.call_tool("approve_application", {"application_id": "app-1"})

    assert result.is_error
    assert "/review" in result.content[0].text
    assert api.posted == []


@BOTH_PROTOCOLS
async def test_the_code_is_typed_by_the_owner(parked, mode) -> None:
    api = parked()
    owner = _Owner(lambda schema: {"code": "481 516"}, mode=mode)

    resumed = await owner.calls("submit_otp", application_id="app-1")

    assert resumed["status"] == "running"
    assert len(owner.shown) == 1
    assert api.posted == [("/applications/app-1/otp", {"code": "481 516"})]


@BOTH_PROTOCOLS
async def test_no_code_is_sent_when_the_owner_closes_the_form(parked, mode) -> None:
    api = parked()
    owner = _Owner(action="decline", mode=mode)

    answered = await owner.calls("submit_otp", application_id="app-1")

    assert api.posted == []
    assert answered["submitted"] is False


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

    # 5. Approve. The open questions are put to the owner in a form; the call
    #    itself carries no answers.
    owner = _Owner(_fills_in_every_field)
    approved = await owner.calls("approve_application", application_id=application_id)
    assert approved["status"] == "running"
    [form] = owner.shown
    assert "Why do you want to work at Acme?" in {f["title"] for f in form["properties"].values()}

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


# --------------------------------------------------------------------------
# Editing the attached résumé over the tool surface
# --------------------------------------------------------------------------


async def _parked_application(client, complete_candidate) -> str:
    """An application at needs_review, which is the only state edits apply to."""
    import uuid as _uuid

    from packages.core.enums import ApplicationStatus
    from packages.core.models import Application

    created = await call("apply_to_url", **complete_candidate, url=APPLY_URL)
    application_id = created["id"]

    from packages.core.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        application = await session.get(Application, _uuid.UUID(application_id))
        application.status = ApplicationStatus.NEEDS_REVIEW.value
        await session.commit()
    return application_id


async def test_inspecting_the_attached_resume_returns_its_lines(client, complete_candidate) -> None:
    """Sections are replaced whole, so an edit has to start from the current set."""
    application_id = await _parked_application(client, complete_candidate)

    attached = await call("inspect_application_resume", application_id=application_id)

    assert attached["editable"] is True
    assert isinstance(attached["sections"]["experience"], list)


async def test_a_tool_edit_that_invents_a_fact_is_refused(client, complete_candidate) -> None:
    """The reason this tool is guarded at all.

    The dashboard editor is not: §2.1 constrains the model, not the owner
    writing their own history. Here the author *is* a model, and an unguarded
    résumé write handed to one is the door §2.1 exists to close.
    """
    application_id = await _parked_application(client, complete_candidate)

    result = await call(
        "edit_application_resume",
        application_id=application_id,
        sections={"experience": ["Principal Engineer at Netflix, cutting latency by 40%."]},
        contact_name="Ada Lovelace",
    )

    assert "error" in result
    assert "does not support" in result["error"]
    assert "Netflix" in result["error"]


async def test_a_relayed_edit_is_stored_and_attached(client, complete_candidate) -> None:
    """The case the tool is for: the owner said to drop a line."""
    application_id = await _parked_application(client, complete_candidate)
    attached = await call("inspect_application_resume", application_id=application_id)

    result = await call(
        "edit_application_resume",
        application_id=application_id,
        sections={"experience": attached["sections"]["experience"]},
        contact_name=attached["contact"].get("name"),
        contact_email=attached["contact"].get("email"),
    )

    assert "error" not in result, result
    assert result["resume_id"] is not None
    # Editing is not approving.
    assert result["status"] == "needs_review"
    assert result["adopted_as_base"] is False


async def test_the_tool_cannot_turn_the_guard_off() -> None:
    """A settable guard is an opt-out from §2.1 by argument.

    The API takes the flag because it cannot tell a person from a model; this
    surface knows the answer and must not offer a choice.
    """
    tools = {t.name: t for t in await mcp_server.server.list_tools()}
    schema = tools["edit_application_resume"].input_schema

    assert "guard" not in schema.get("properties", {})
