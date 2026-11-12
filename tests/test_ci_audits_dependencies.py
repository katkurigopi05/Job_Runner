"""CI looks for known advisories in what is pinned, on a schedule.

On 2026-11-11 `pip-audit` found 65 advisories in seven pins of
`constraints.txt`. Nothing had been looking: the lock holds versions still,
which is its job, and a pinned version does not hear about its own advisory.

The job runs weekly and on demand, never on a pull request: an advisory
published overnight is not something the pull request caused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _audit_job() -> dict[str, Any]:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    job: dict[str, Any] = workflow["jobs"]["audit"]
    return job


def _commands() -> str:
    return "\n".join(step.get("run", "") for step in _audit_job()["steps"])


def test_python_pins_are_audited_from_the_lock() -> None:
    assert "pip-audit" in _commands()
    assert "-r constraints.txt" in _commands()


def test_dashboard_lockfile_is_audited() -> None:
    assert "npm audit" in _commands()


def test_the_audit_never_runs_on_a_pull_request() -> None:
    condition = _audit_job()["if"]
    assert "schedule" in condition and "workflow_dispatch" in condition
    assert "pull_request" not in condition


def test_the_audit_token_can_only_read() -> None:
    """CodeQL flagged the job for leaving the token's permissions at the default."""
    assert _audit_job()["permissions"] == {"contents": "read"}


def test_dependabot_leaves_the_python_lock_to_make_lock() -> None:
    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text(encoding="utf-8"))
    ecosystems = {update["package-ecosystem"] for update in config["updates"]}
    assert ecosystems == {"github-actions", "npm"}
