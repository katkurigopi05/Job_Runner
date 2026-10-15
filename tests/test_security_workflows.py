"""Security evidence is explicit; an advisory timeout still gets reported."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_ai_review_leaves_time_to_report_an_incomplete_review() -> None:
    job = yaml.safe_load((ROOT / ".github/workflows/ai-review.yml").read_text())["jobs"]["review"]
    review = next(step for step in job["steps"] if step.get("id") == "review")
    assert 0 < review.get("timeout-minutes", 0) < job["timeout-minutes"]
    assert review["continue-on-error"] is True
    report = next(step for step in job["steps"] if step.get("name", "").startswith("Report"))
    assert "always()" in report["if"]
    assert "failure" in report["if"] and "cancelled" in report["if"]
    assert "not** been reviewed" in report["run"]


def test_security_regressions_are_required_and_published() -> None:
    job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["gates"]
    steps = job["steps"]
    assert any(step.get("uses", "").startswith("actions/setup-node@") for step in steps)
    gate = next(step for step in steps if step.get("id") == "security")
    assert not gate.get("continue-on-error", False)
    assert gate["env"]["REQUIRE_SECURITY_NODE"] == "1"
    for file in (
        "test_security_origins.py",
        "test_rebinding_guard.py",
        "test_middleware.py",
        "test_database_binds_loopback.py",
    ):
        assert file in gate["run"]
    assert "--junitxml=" in gate["run"]
    artifact = next(step for step in steps if step.get("name") == "Security regression evidence")
    assert artifact["if"] == "always()"
    assert artifact["uses"].startswith("actions/upload-artifact@")
    assert "security-regressions.xml" in artifact["with"]["path"]
