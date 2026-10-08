"""The audit routes, over HTTP.

`test_llm_audit.py` covers the trail itself. What it could not see is that
`apps/api/routers/audit.py` was never reachable: `main.py` never included the
router, and adding the include raised `ImportError` because
`AuditEntryOut`, `AuditSummaryOut` and `AuditVerifyRequest` did not exist in
any branch, and `audit.digest_of` did not either.

None of that failed a gate. Gate 0 runs the whole suite and the suite never
imported the module, so three routes that CLAUDE.md §2.8 relies on for
"the owner can audit what left the machine" returned 404 for their whole life.
`test_analytics_api.py` warned about exactly this shape — an endpoint with no
caller and no test is one nobody finds out is broken.

So these tests are about the boundary: that the routes are mounted, that the
declared `response_model` matches what the handler returns, and — the part
with consequences — that no prompt text is ever in a response.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from packages.llm import audit

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolated_trail(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "audit_path", lambda: tmp_path / "llm-audit.jsonl")


SYSTEM = "You rewrite resume bullets."
USER = "Built backend services in Python."


def _record_one(*, provider: str = "gemini", model: str | None = "gemini-2.0-flash"):
    return audit.record(provider, SYSTEM, USER, task="tailor_resume", model=model)


async def test_the_routes_are_mounted(client: AsyncClient) -> None:
    """The regression that started this. A 404 here means an unmounted router."""
    for path in ("/audit", "/audit/summary"):
        assert (await client.get(path)).status_code == 200, f"{path} is not mounted"


async def test_a_recorded_call_is_listed(client: AsyncClient) -> None:
    _record_one()
    body = (await client.get("/audit")).json()

    assert len(body) == 1
    assert body[0]["provider"] == "gemini"
    assert body[0]["task"] == "tailor_resume"
    assert body[0]["left_machine"] is True


async def test_no_response_ever_carries_prompt_text(client: AsyncClient) -> None:
    """§10 forbids logging résumé contents and the trail stores none.

    An endpoint able to return the prompt would mean the file contained it, so
    this asserts against the *response*, not the storage format.
    """
    _record_one()
    for path in ("/audit", "/audit/summary"):
        raw = (await client.get(path)).text
        assert SYSTEM not in raw
        assert USER not in raw

    raw = (await client.post("/audit/verify", json={"text": USER})).text
    assert USER not in raw, "verify echoed the submitted text back"


async def test_uploads_only_narrows_to_what_left_the_machine(client: AsyncClient) -> None:
    _record_one(provider="ollama", model="llama3.1")
    _record_one()

    everything = (await client.get("/audit")).json()
    uploads = (await client.get("/audit", params={"uploads_only": True})).json()

    assert len(everything) == 2, "a local call is still recorded and still listed"
    assert [e["provider"] for e in uploads] == ["gemini"]


async def test_the_summary_counts_only_uploads_in_its_totals(client: AsyncClient) -> None:
    _record_one(provider="ollama", model="llama3.1")
    _record_one()

    body = (await client.get("/audit/summary")).json()

    assert body["total_calls"] == 2
    assert body["uploads"] == 1
    assert body["uploaded_chars"] == len(USER)
    assert body["by_provider"] == {"gemini/gemini-2.0-flash": 1}


async def test_verify_finds_the_entry_for_text_you_hold(client: AsyncClient) -> None:
    """The whole audit story: holding the original, you can prove what was sent."""
    _record_one()

    found = (await client.post("/audit/verify", json={"text": USER})).json()
    assert len(found) == 1
    assert found[0]["user_sha256"] == audit.digest_of(USER)

    missed = (await client.post("/audit/verify", json={"text": "never sent"})).json()
    assert missed == []


async def test_the_limit_is_bounded(client: AsyncClient) -> None:
    """400 with the §10 envelope, not FastAPI's bare 422.

    `apps/api/errors.py` normalises validation failures into the shared
    `{"error": {"code", "message"}}` shape, so a client parsing one error
    parses all of them. Asserting 422 here would have pinned the framework's
    default over this app's contract.
    """
    for bad in (0, 99999):
        response = await client.get("/audit", params={"limit": bad})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"


async def test_an_empty_trail_is_not_an_error(client: AsyncClient) -> None:
    """Nothing sent anywhere is the shipped default, not a failure state."""
    assert (await client.get("/audit")).json() == []
    summary = (await client.get("/audit/summary")).json()
    assert summary["total_calls"] == 0
    assert summary["first_at"] is None


# --------------------------------------------------------------------------
# A window
# --------------------------------------------------------------------------
#
# "What left my machine this week?" could not be asked. The summary was the
# whole trail or nothing.


def _backdate(index: int, days: int) -> None:
    """Rewrite one recorded entry as if it had been made `days` ago."""
    import json
    from datetime import UTC, datetime, timedelta

    path = audit.audit_path()
    lines = [json.loads(line) for line in path.read_text().splitlines() if line]
    lines[index]["at"] = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))


async def test_the_summary_can_be_narrowed_to_recent_days(client: AsyncClient) -> None:
    _record_one()
    _record_one()
    _backdate(0, days=10)

    week = (await client.get("/audit/summary", params={"days": 7})).json()
    month = (await client.get("/audit/summary", params={"days": 30})).json()
    whole = (await client.get("/audit/summary")).json()

    assert (week["uploads"], month["uploads"], whole["uploads"]) == (1, 2, 2)
    assert week["uploaded_chars"] == len(USER)


async def test_the_summary_says_which_tasks_uploaded(client: AsyncClient) -> None:
    """§2.8 permits one upload, the tailoring call. A count by provider cannot
    show an upload made for something else."""
    _record_one()
    audit.record("gemini", SYSTEM, USER, task="assistant", model="gemini-2.0-flash")
    audit.record("ollama", SYSTEM, USER, task="classify_inbound_email", model="llama3.1")

    body = (await client.get("/audit/summary")).json()

    assert body["by_task"] == {"tailor_resume": 1, "assistant": 1}


async def test_an_entry_whose_time_cannot_be_read_is_still_counted(client: AsyncClient) -> None:
    """Leaving an upload out of the answer is the worse of the two mistakes."""
    import json

    _record_one()
    path = audit.audit_path()
    entry = json.loads(path.read_text().splitlines()[0])
    path.write_text(json.dumps({**entry, "at": "sometime"}) + "\n")

    assert (await client.get("/audit/summary", params={"days": 7})).json()["uploads"] == 1


async def test_the_window_is_bounded(client: AsyncClient) -> None:
    for bad in (0, 99999):
        response = await client.get("/audit/summary", params={"days": bad})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
