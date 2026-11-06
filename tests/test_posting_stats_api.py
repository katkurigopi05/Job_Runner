"""`POST /postings/stats` and the `posting_stats` MCP tool, end to end.

Tool → HTTP client → the real FastAPI app → Postgres, as in `test_mcp.py`.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from httpx import ASGITransport

from apps.mcp import server as mcp_server
from apps.mcp.client import JobrunnerClient
from packages.core.models import Company, Posting
from packages.matching.stats_schema import GroupBy, Place, WorkMode


@pytest.fixture
async def california_postings(committing_sessionmaker) -> None:
    async with committing_sessionmaker() as session:
        company = Company(name="ExampleCo " + uuid.uuid4().hex[:8], ats_type="greenhouse")
        session.add(company)
        await session.flush()
        for title, location in (
            ("Data Engineer", "San Francisco, CA"),
            ("Software Engineer", "San Diego, CA"),
            ("Data Engineer", "Austin, TX"),
        ):
            session.add(
                Posting(
                    company_id=company.id,
                    title=title,
                    location=location,
                    url="https://jobs.example.test/" + uuid.uuid4().hex,
                    description_raw="A role.",
                    content_hash=uuid.uuid4().hex,
                )
            )
        await session.commit()


@pytest.fixture
def _bind_tools_to_app(client, monkeypatch) -> None:
    from apps.api.main import app

    bound = JobrunnerClient(
        base_url="http://localhost", transport=ASGITransport(app=app, client=("127.0.0.1", 1))
    )
    monkeypatch.setattr(mcp_server, "_client", bound)


async def call(name: str, **arguments: Any) -> Any:
    result = await mcp_server.server.call_tool(name, arguments)
    assert not result.is_error, result.content
    if result.structured_content and "result" in result.structured_content:
        return result.structured_content["result"]
    return json.loads(result.content[0].text)


async def test_api_answers_a_california_share(client, california_postings):
    # Act
    response = await client.post(
        "/postings/stats", json={"place": "california", "share_roles": ["data engineer"]}
    )

    # Assert
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["total"], body["matching"], body["percentage"]) == (2, 1, 50.0)
    assert "California" in body["scope"]


async def test_api_refuses_an_unknown_role_in_the_error_envelope(client):
    response = await client.post("/postings/stats", json={"roles": ["underwater basket weaver"]})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert "underwater basket weaver" in error["message"]


async def test_mcp_tool_returns_the_same_counts(_bind_tools_to_app, california_postings):
    result = await call("posting_stats", place="california", group_by="role")

    assert result["total"] == 2
    assert [group["count"] for group in result["groups"]] == [1, 1]


def _choices(schema: dict[str, Any]) -> list[str]:
    """An enum's values, inline or behind an optional's `anyOf`."""
    if "enum" in schema:
        return list(schema["enum"])
    return [value for option in schema.get("anyOf", []) for value in option.get("enum", [])]


async def test_mcp_tool_offers_exactly_the_schema_choices():
    tools = {tool.name: tool for tool in await mcp_server.server.list_tools()}
    tool = tools["posting_stats"]
    properties = tool.input_schema["properties"]

    assert "computed" in tool.description.lower()
    assert _choices(properties["place"]) == [place.value for place in Place]
    assert _choices(properties["work_mode"]) == [mode.value for mode in WorkMode]
    assert _choices(properties["share_work_mode"]) == [mode.value for mode in WorkMode]
    assert _choices(properties["group_by"]) == [group.value for group in GroupBy]
