"""The TypeScript parser must not regain the globparts amplification bug."""

import json
from pathlib import Path

from packaging.version import Version

WEB = Path(__file__).resolve().parents[1] / "apps/web"


def test_typescript_parser_resolves_patched_minimatch() -> None:
    """Resolve nested or hoisted npm dependencies, not a particular override."""
    packages = json.loads((WEB / "package-lock.json").read_text())["packages"]
    locations = (
        "node_modules/@typescript-eslint/typescript-estree/node_modules/minimatch",
        "node_modules/@typescript-eslint/node_modules/minimatch",
        "node_modules/minimatch",
    )
    resolved = next(packages[location] for location in locations if location in packages)
    assert Version(resolved["version"]) >= Version("11.0.1"), (
        "The TypeScript parser resolves minimatch affected by CVE-2026-118764"
    )
