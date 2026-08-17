"""The audit trail — CLAUDE.md §2.8, without breaking §10."""

from __future__ import annotations

import pytest

from packages.llm import audit
from packages.llm.provider import StubProvider


@pytest.fixture(autouse=True)
def _isolated_trail(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "audit_path", lambda: tmp_path / "llm-audit.jsonl")
    yield


RESUME = "Ada Lovelace — Staff Engineer, Analytical Engines Ltd. Python, PostgreSQL."


async def test_a_call_is_recorded() -> None:
    await StubProvider().complete("tailor this résumé", RESUME)

    trail = audit.read_trail()
    assert len(trail) == 1
    assert trail[0].provider == "stub"


async def test_the_resume_text_is_not_written_to_the_trail(tmp_path) -> None:
    """§10 — the audit record must not become a second copy of the résumé.

    §2.8 wants proof of what left the machine. A digest gives that. Storing the
    text itself would make the audit file a copy nobody chose to make.
    """
    await StubProvider().complete("system", RESUME)

    raw = audit.audit_path().read_text()
    assert "Ada Lovelace" not in raw
    assert "Analytical Engines" not in raw


async def test_the_owner_can_prove_what_was_sent() -> None:
    """Holding the original, an entry can be confirmed against it."""
    await StubProvider().complete("system", RESUME)

    entry = audit.read_trail()[0]
    assert entry.matches("system", RESUME)
    assert not entry.matches("system", RESUME + " and a line nobody wrote")


async def test_local_providers_are_marked_as_not_leaving() -> None:
    """§2.8 is about third-party upload. Ollama and the stub are local."""
    await StubProvider().complete("system", RESUME)

    entry = audit.read_trail()[0]
    assert entry.left_machine is False
    assert audit.uploads_only() == []
