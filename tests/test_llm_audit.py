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


# --------------------------------------------------------------------------
# Cloud models served through Ollama — §2.8
# --------------------------------------------------------------------------


import pytest  # noqa: E402

from packages.llm.audit import is_local  # noqa: E402


@pytest.mark.parametrize(
    ("provider", "model", "local"),
    [
        ("ollama", "llama3.1", True),
        ("ollama", "mistral:latest", True),
        ("stub", None, True),
        # Both spellings. `kimi-k2.6:cloud` and `qwen3-coder:480b-cloud` are
        # not on disk; Ollama serves them from its own servers under the same
        # API. Matching only ":cloud" missed the second.
        ("ollama", "kimi-k2.6:cloud", False),
        ("ollama", "qwen3-coder:480b-cloud", False),
        ("gemini", "gemini-2.0", False),
        ("anthropic", "claude", False),
    ],
)
def test_locality_depends_on_the_model_not_only_the_provider(
    provider: str, model: str | None, local: bool
) -> None:
    """§2.8 — the audit must not say a résumé stayed here when it did not.

    Judging by provider name alone recorded a call to Ollama's cloud as
    `left_machine=False`. That is the one failure this file exists to prevent:
    an audit confidently answering its only question incorrectly.
    """
    assert is_local(provider, model) is local


def test_over_reporting_is_the_safe_direction() -> None:
    """A local model named "cloudy" would be recorded as having left.

    Wrong, and wrong in the direction that cannot hurt: it overstates what
    left the machine rather than understating it.
    """
    assert is_local("ollama", "cloudy-local-model") is False


async def test_a_cloud_model_is_recorded_as_having_left(tmp_path) -> None:
    from packages.llm import audit

    entry = audit.record("ollama", "system", "résumé text", model="kimi-k2.6:cloud")

    assert entry.left_machine is True
    assert entry.model == "kimi-k2.6:cloud"
    assert audit.uploads_only([entry]) == [entry]
