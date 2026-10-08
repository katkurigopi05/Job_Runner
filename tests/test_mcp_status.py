"""Status questions over MCP: what left the machine, is the setup healthy,
what happened this week, is the crawler working, and starting a crawl.

The first four read. The fifth queues work that makes real requests to
employers' sites, so it is one of the tools that make the client ask the owner.

Driven the way `tests/test_mcp.py` drives the rest: tool, HTTP client, the real
FastAPI app, Postgres.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest

from apps.mcp import server as mcp_server
from packages.llm import audit

# The binding to the ASGI app is the other file's, so the two cannot differ.
from tests.test_mcp import _bind_tools_to_app, _wire, call  # noqa: F401

SYSTEM = "You rewrite resume bullets."
USER = "Built backend services in Python."


@pytest.fixture(autouse=True)
def _isolated_trail(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "audit_path", lambda: tmp_path / "llm-audit.jsonl")


class _Recording:
    """Stands in for the API: what was asked of it, and a reply of the right shape."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str, dict[str, Any], Any]] = []

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.asked.append((method, path, kwargs.get("params") or {}, kwargs.get("json")))
        if path == "/setup/status":
            return {"overall": "ok", "items": []}
        if path == "/crawl":
            return {"queued": True, "already_waiting": 0, "worker_alive": True}
        return {}


@pytest.fixture
def api(monkeypatch) -> _Recording:
    fake = _Recording()
    monkeypatch.setattr(mcp_server, "_client", fake)
    return fake


# --------------------------------------------------------------------------
# What left the machine
# --------------------------------------------------------------------------


async def test_the_trail_says_what_left_to_whom_and_for_what() -> None:
    audit.record("gemini", SYSTEM, USER, task="tailor_resume", model="gemini-2.0-flash")
    audit.record("ollama", SYSTEM, USER, task="assistant", model="qwen3")

    left = await call("audit_trail")

    assert left["uploads"] == 1 and left["total_calls"] == 2
    assert left["uploaded_chars"] == len(USER)
    assert left["by_provider"] == {"gemini/gemini-2.0-flash": 1}
    assert left["by_task"] == {"tailor_resume": 1}
    assert left["days"] == 7
    assert "note" not in left


async def test_the_trail_carries_no_text_and_no_digest() -> None:
    """The trail stores digests so the owner, holding the original, can prove
    what was sent. A digest is no use to an assistant, and the text was never
    stored."""
    audit.record("gemini", SYSTEM, USER, task="tailor_resume", model="gemini-2.0-flash")

    sent = json.dumps(await call("audit_trail"))

    assert SYSTEM not in sent and USER not in sent
    assert audit.digest_of(USER) not in sent and "sha256" not in sent


async def test_nothing_uploaded_is_said_and_not_left_as_a_zero() -> None:
    audit.record("ollama", SYSTEM, USER, task="assistant", model="qwen3")

    left = await call("audit_trail", days=30)

    assert left["uploads"] == 0 and left["total_calls"] == 1
    assert "this machine" in left["note"] and "30" in left["note"]


async def test_a_window_the_api_refuses_is_refused_not_ignored() -> None:
    left = await call("audit_trail", days=0)

    assert left["code"] == "invalid_request"
    assert "uploads" not in left


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------


async def test_setup_health_sorts_every_item_into_fine_or_not(client) -> None:
    reported = (await client.get("/setup/status")).json()

    health = await call("setup_health")

    assert health["overall"] == reported["overall"]
    wrong = {item["key"] for item in health["needs_attention"]}
    assert wrong == {item["key"] for item in reported["items"] if item["state"] != "ok"}
    assert sorted(health["ok"]) == sorted(
        item["title"] for item in reported["items"] if item["state"] == "ok"
    )
    assert len(wrong) + len(health["ok"]) == len(reported["items"]) > 0


async def test_an_item_that_needs_attention_comes_with_what_to_do(client) -> None:
    health = await call("setup_health")

    assert health["needs_attention"], "a test database with no worker has something wrong"
    for item in health["needs_attention"]:
        # The exact set, so adding a field is a deliberate act.
        assert set(item) == {"key", "title", "group", "state", "detail", "steps"}


# --------------------------------------------------------------------------
# The week
# --------------------------------------------------------------------------


async def test_the_digest_of_an_empty_week_says_it_was_quiet() -> None:
    week = await call("weekly_digest")

    assert week["quiet_week"] is True
    assert week["window_days"] == 7 and week["applications_submitted"] == 0


async def test_the_digests_window_is_the_reports_own_unless_one_is_given(api) -> None:
    await call("weekly_digest")
    await call("weekly_digest", window_days=30, profile_id="p-1")

    assert [params for _, _, params, _ in api.asked] == [
        {},
        {"window_days": 30, "profile_id": "p-1"},
    ]


# --------------------------------------------------------------------------
# The crawler
# --------------------------------------------------------------------------


async def test_crawl_status_is_the_dashboards_own() -> None:
    status = await call("crawl_status")

    assert status["running"] is False and status["pending"] == 0
    assert "newest_posting_at" in status


async def test_starting_a_crawl_with_no_worker_says_nothing_will_run_it() -> None:
    started = await call("start_crawl")

    assert started["queued"] is True and started["worker_alive"] is False
    assert "make worker" in started["note"]
    assert (await call("crawl_status"))["pending"] == 1


async def test_a_second_start_queues_nothing_and_says_so() -> None:
    await call("start_crawl")
    again = await call("start_crawl")

    assert again["queued"] is False and again["already_waiting"] == 1
    assert "already" in again["note"]
    assert (await call("crawl_status"))["pending"] == 1


async def test_a_crawl_with_a_worker_up_says_when_postings_arrive(api) -> None:
    started = await call("start_crawl")

    assert started["queued"] is True
    assert "make worker" not in started["note"] and "feed" in started["note"]


async def test_starting_a_crawl_asks_the_owner_every_time() -> None:
    """A crawl makes real requests to every employer's board in the registry.
    The assistant's "run crawler" is a typed command matched in code, never
    inferred from a question (§14). Over MCP the one deciding is a model, and
    an empty feed is exactly what would make one decide to. So the client asks."""
    tools = {t.name: _wire(t) for t in await mcp_server.server.list_tools()}

    assert "start_crawl" in mcp_server.ASKS_THE_OWNER
    assert tools["start_crawl"]["_meta"][mcp_server.ASK_FLAG] is True


# --------------------------------------------------------------------------
# What is sent
# --------------------------------------------------------------------------

READS = ("audit_trail", "setup_health", "weekly_digest", "crawl_status")


async def test_the_status_tools_read_and_the_crawl_tool_posts_one_thing(api) -> None:
    for tool in READS:
        await call(tool)
    assert {method for method, _, _, _ in api.asked} == {"GET"}
    assert {path for _, path, _, _ in api.asked} == {
        "/audit/summary",
        "/setup/status",
        "/analytics/digest",
        "/crawl/status",
    }

    api.asked.clear()
    await call("start_crawl")
    # No body: there is nothing about a crawl for a model to choose.
    assert api.asked == [("POST", "/crawl", {}, None)]


async def test_no_status_tool_can_repair_the_registry() -> None:
    """`POST /setup/registry-sync` rewrites company rows. It stays on the
    setup page, where the owner previews it first."""
    source = inspect.getsource(mcp_server)

    assert "registry-sync" not in source and "/audit/verify" not in source


async def test_every_parameter_sent_is_one_its_route_reads(api) -> None:
    """FastAPI ignores a query parameter it does not know. A window spelt
    wrong here would be sent, dropped, and answered as if nobody had asked."""
    from apps.api.routers import analytics
    from apps.api.routers import audit as audit_route

    await call("audit_trail", days=30)
    await call("weekly_digest", window_days=30, profile_id="p-1")

    reads = {
        "/audit/summary": set(inspect.signature(audit_route.summary).parameters),
        "/analytics/digest": set(inspect.signature(analytics.read_digest).parameters),
    }
    for _, path, params, _ in api.asked:
        assert params and params.keys() <= reads[path], (path, params.keys() - reads[path])
