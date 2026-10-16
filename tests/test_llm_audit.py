"""The audit trail — CLAUDE.md §2.8, without breaking §10."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from packages.llm import audit, quota
from packages.llm.audit import AuditUnavailable
from packages.llm.provider import GeminiProvider, StubProvider
from packages.llm.quota import QuotaExceeded


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


def test_remote_call_fails_before_network_when_audit_cannot_persist(monkeypatch) -> None:
    def unavailable():
        raise OSError("private path must never appear in the public error")

    monkeypatch.setattr(audit, "audit_path", unavailable)

    with pytest.raises(AuditUnavailable) as excinfo:
        audit.record("gemini", "system", "resume")

    assert "private path" not in str(excinfo.value)


async def test_remote_provider_is_not_contacted_when_audit_cannot_persist(monkeypatch) -> None:
    contacted = False

    def unavailable():
        raise OSError("read-only")

    async def post(*args, **kwargs):
        nonlocal contacted
        contacted = True
        raise AssertionError("the request must not leave")

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(audit, "audit_path", unavailable)
    monkeypatch.setattr(httpx.AsyncClient, "post", post)

    with pytest.raises(AuditUnavailable):
        await GeminiProvider().complete("system", "resume")

    assert contacted is False


def test_local_call_continues_when_audit_cannot_persist(monkeypatch) -> None:
    def unavailable():
        raise OSError("read-only")

    monkeypatch.setattr(audit, "audit_path", unavailable)

    entry = audit.record("ollama", "system", "local prompt", model="qwen3:8b")

    assert entry.left_machine is False


def test_ollama_cloud_tag_uses_remote_failure_policy(monkeypatch) -> None:
    def unavailable():
        raise OSError("read-only")

    monkeypatch.setattr(audit, "audit_path", unavailable)

    with pytest.raises(AuditUnavailable):
        audit.record("ollama", "system", "resume", model="kimi-k2.6:cloud")


def test_local_ollama_calls_do_not_consume_a_cloud_tagged_models_quota(monkeypatch) -> None:
    monkeypatch.setattr(
        quota,
        "limit_for",
        lambda provider, *, left_machine=None: 1 if left_machine else None,
    )
    audit.record("ollama", "system", "local", model="qwen3:8b")

    remote = audit.record("ollama", "system", "remote", model="kimi-k2.6:cloud")

    assert remote.left_machine is True
    with pytest.raises(QuotaExceeded):
        audit.record("ollama", "system", "another", model="kimi-k2.6:cloud")


def test_concurrent_remote_calls_cannot_overspend_the_daily_limit(monkeypatch) -> None:
    monkeypatch.setattr(
        quota,
        "limit_for",
        lambda provider, *, left_machine=None: 3 if left_machine else None,
    )

    def call(number: int) -> str:
        try:
            audit.record("gemini", "system", f"resume-{number}")
        except QuotaExceeded:
            return "refused"
        return "recorded"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(call, range(8)))

    assert outcomes.count("recorded") == 3
    assert outcomes.count("refused") == 5
    assert len(audit.read_trail()) == 3


def test_audit_retention_keeps_only_the_last_365_days() -> None:
    old = audit.record("stub", "system", "old")
    recent = audit.record("stub", "system", "recent")
    old_at = (datetime.now(UTC) - timedelta(days=366)).isoformat()
    audit.audit_path().write_text(
        "\n".join((json.dumps(asdict(replace(old, at=old_at))), json.dumps(asdict(recent)))) + "\n",
        encoding="utf-8",
    )

    audit.enforce_retention()

    assert [entry.user_sha256 for entry in audit.read_trail()] == [recent.user_sha256]


def test_a_call_keeps_its_request_and_worker_correlation() -> None:
    from packages.core.trace import correlated

    with correlated(trace_id="c960a1f2-83cf-42da-b849-5f6ec2398e5c", task_id="task-7"):
        entry = audit.record("stub", "system", "user")

    assert entry.event_version == 2
    assert entry.trace_id == "c960a1f2-83cf-42da-b849-5f6ec2398e5c"
    assert entry.task_id == "task-7"
