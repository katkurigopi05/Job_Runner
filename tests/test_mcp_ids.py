"""An id handed to a tool cannot name another route.

A tool that takes an id puts it into the path it asks the local API for, and
httpx resolves `..` before it sends. So `application_status` given `../inbox?`
in place of an application's id asked for `GET /inbox`: the recruiter mail this
server deliberately has no tool for. The one calling is a model, and a posting
or a reply it has read can tell it what to pass.

The first half runs every tool against a stand-in that records what was asked
of it. The second is the real app. The third reads the source, so a tool
written later cannot leave the check out.
"""

from __future__ import annotations

import ast
import inspect
import re
from typing import Any

import httpx
import pytest
from mcp import types
from mcp.client import Client

from apps.mcp import server as mcp_server

# The binding to the ASGI app is the other file's, so the two cannot differ.
from tests.test_mcp import _bind_tools_to_app  # noqa: F401

#: What a model could pass in place of an id, and where each one goes.
CRAFTED = [
    "../inbox?",  # another route, with the rest of the path made a query
    "../contacts#",  # another route, with the rest made a fragment
    "x/tracking",  # a deeper route under the same id
    "..%2Finbox",  # the same, encoded
    "..",
    "a b",
    "",
]

_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+")


def _is_plain(path: str) -> bool:
    """Every part of the path is a name, and none of them climbs."""
    parts = path.split("/")[1:]
    return (
        path.startswith("/")
        and bool(parts)
        and all(_SEGMENT.fullmatch(part) and part not in (".", "..") for part in parts)
    )


class _Recording:
    """Stands in for the API: every path it was asked for."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.paths.append(path)
        return {}


async def _accepts(context: Any, params: Any) -> types.ElicitResult:
    """An owner who fills in whatever form they are shown and sends it."""
    asked = params.model_dump(by_alias=True, exclude_none=True)["requestedSchema"]
    return types.ElicitResult(
        action="accept", content=dict.fromkeys(asked.get("properties", {}), "123456")
    )


def _filler(schema: dict[str, Any]) -> Any:
    kind = schema.get("type")
    return {"integer": 1, "number": 1, "boolean": False, "array": [], "object": {}}.get(kind, "x")


async def _tools_that_take_an_id() -> list[tuple[str, str, dict[str, Any]]]:
    """(tool, the id's name, the rest of what the tool requires)."""
    found = []
    for tool in await mcp_server.server.list_tools():
        properties = tool.input_schema.get("properties", {})
        required = tool.input_schema.get("required", [])
        for name in properties:
            if name.endswith("_id"):
                others = {r: _filler(properties[r]) for r in required if r != name}
                found.append((tool.name, name, others))
    return found


async def _asked_for(tool: str, arguments: dict[str, Any], monkeypatch) -> list[str]:
    api = _Recording()
    monkeypatch.setattr(mcp_server, "_client", api)
    async with Client(mcp_server.server, elicitation_callback=_accepts) as client:
        await client.call_tool(tool, arguments)
    return api.paths


async def test_the_tools_that_take_an_id_are_the_ones_being_checked() -> None:
    """If this list shrinks, the test below is checking less than it says."""
    taking = {(tool, name) for tool, name, _ in await _tools_that_take_an_id()}

    assert ("application_status", "application_id") in taking
    assert ("inspect_resume", "resume_id") in taking
    assert ("curate_project", "project_id") in taking
    assert len(taking) >= 15


async def test_a_real_id_reaches_the_api(monkeypatch) -> None:
    """The control. A tool whose made-up arguments were refused before it ran
    would pass the next test without having been tried."""
    for tool, name, others in await _tools_that_take_an_id():
        paths = await _asked_for(tool, {**others, name: "app-1"}, monkeypatch)

        assert paths, f"{tool} asked the API for nothing"
        assert all(_is_plain(path) for path in paths), (tool, paths)


@pytest.mark.parametrize("crafted", CRAFTED)
async def test_an_id_that_is_not_one_names_no_other_route(crafted, monkeypatch) -> None:
    for tool, name, others in await _tools_that_take_an_id():
        paths = await _asked_for(tool, {**others, name: crafted}, monkeypatch)

        assert all(_is_plain(path) for path in paths), (tool, name, paths)
        if crafted:
            assert not any(crafted in path for path in paths), (tool, name, paths)


def test_the_path_that_was_being_sent() -> None:
    """Why the check is needed at all: this is httpx, not the API."""
    sent = httpx.Request("GET", "http://127.0.0.1:8000/applications/../inbox?/tracking")

    assert sent.url.path == "/inbox"


# --------------------------------------------------------------------------
# Against the real app
# --------------------------------------------------------------------------


async def test_another_route_cannot_be_read_through_a_tool(complete_candidate) -> None:
    """`../candidates?` for an application's id returned the candidate list as
    that application's history, the owner's name and email with it."""
    async with Client(mcp_server.server) as client:
        result = await client.call_tool("application_history", {"application_id": "../candidates?"})

    said = result.content[0].text
    assert result.is_error, said
    assert "application_id" in said
    assert "@example.com" not in said and "email" not in said


async def test_a_models_edit_cannot_be_sent_to_the_unguarded_route(
    client, complete_candidate
) -> None:
    """`edit_application_resume` runs the fabrication guard because a model is
    typing (§2.1). The résumés page's own edit route does not, because there
    the owner is. Both take the same body. With a résumé's id written as
    `../resumes/<id>/edit?`, the tool's edit went to that route instead: an
    invented line, stored as a new version of the résumé, checked by nothing."""
    owned = {"candidate_id": complete_candidate["candidate_id"]}
    before = (await client.get("/resumes", params=owned)).json()

    async with Client(mcp_server.server) as assistant:
        result = await assistant.call_tool(
            "edit_application_resume",
            {
                "application_id": f"../resumes/{before[0]['id']}/edit?",
                "sections": {"experience": ["VP of Engineering at Globex, led a team of 40"]},
            },
        )

    after = (await client.get("/resumes", params=owned)).json()
    # What was stored first: a refusal that came after the write would be no use.
    assert len(after) == len(before), "the edit was stored as a new version"
    assert result.is_error


async def test_a_refused_id_is_named_and_not_repeated() -> None:
    crafted = "../inbox?"
    async with Client(mcp_server.server) as client:
        result = await client.call_tool("inspect_resume", {"resume_id": crafted})

    said = result.content[0].text
    assert result.is_error
    assert "resume_id" in said and crafted not in said


async def test_an_id_the_api_does_not_know_is_still_the_apis_to_refuse() -> None:
    """The check keeps an id to one part of a path. Whether it is anybody's id
    is the API's to say, with its own error."""
    async with Client(mcp_server.server) as client:
        result = await client.call_tool("application_status", {"application_id": "not-a-uuid"})

    assert not result.is_error
    assert "invalid_request" in result.content[0].text


# --------------------------------------------------------------------------
# In the source
# --------------------------------------------------------------------------


def test_nothing_is_put_into_a_path_unchecked() -> None:
    """Every value formatted into a path goes through `_id`. Read from the
    source, because a tool added later is not in any list above until it
    exists, and the tool this was found beside had left the check out."""
    tree = ast.parse(inspect.getsource(mcp_server))

    paths = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        and node.values
        and isinstance(node.values[0], ast.Constant)
        and str(node.values[0].value).startswith("/")
    ]
    assert len(paths) >= 14, "the paths were not found, so nothing was checked"

    unchecked = [
        ast.unparse(node)
        for node in paths
        for part in node.values
        if isinstance(part, ast.FormattedValue)
        and not (
            isinstance(part.value, ast.Call)
            and isinstance(part.value.func, ast.Name)
            and part.value.func.id == "_id"
        )
    ]
    assert unchecked == []
