"""The dashboard is checked in CI, with the commands a developer runs.

`npm run lint` exited 2 for as long as nobody ran it, because there was no
ESLint config and no CI step that would have noticed. These assertions keep the
job from being quietly deleted or narrowed to one of its three checks.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _frontend_job() -> dict:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    return workflow["jobs"]["frontend"]


def test_the_dashboard_job_runs_lint_types_and_build() -> None:
    job = _frontend_job()
    commands = [step.get("run", "") for step in job["steps"]]

    assert job["defaults"]["run"]["working-directory"] == "apps/web"
    for expected in (
        "npm ci",
        "npm test",
        "npm run lint",
        "npm run typecheck",
        "npm run build",
        "npm run test:e2e",
    ):
        assert expected in commands, expected


def test_the_job_runs_on_pull_requests() -> None:
    """Unlike the image job, which is metered and skipped on PRs."""
    assert "if" not in _frontend_job()


def test_every_script_the_job_calls_exists() -> None:
    import json

    scripts = json.loads((ROOT / "apps/web/package.json").read_text())["scripts"]
    for name in ("test", "lint", "typecheck", "build", "test:e2e"):
        assert name in scripts, name


def test_eslint_has_a_config_to_read() -> None:
    assert (ROOT / "apps/web/eslint.config.mjs").is_file()


def test_browser_tests_use_an_isolated_api_and_pinned_dev_dependency() -> None:
    import json
    import re

    package = json.loads((ROOT / "apps/web/package.json").read_text())
    assert "@playwright/test" not in package["dependencies"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", package["devDependencies"]["@playwright/test"])
    config = (ROOT / "apps/web/playwright.config.ts").read_text()
    assert "127.0.0.1" in config and "reuseExistingServer: false" in config
    steps = _frontend_job()["steps"]
    e2e = next(step for step in steps if step.get("run") == "npm run test:e2e")
    assert e2e["env"]["E2E_MODE"] == "start"
