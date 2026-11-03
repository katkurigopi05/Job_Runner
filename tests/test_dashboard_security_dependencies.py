"""No installed minimatch engine may regain the globparts amplification bug."""

import json
from pathlib import Path

from packaging.version import Version

WEB = Path(__file__).resolve().parents[1] / "apps/web"


def test_every_minimatch_engine_is_patched() -> None:
    """Check nested, hoisted and aliased packages; local links are not engines."""
    packages = json.loads((WEB / "package-lock.json").read_text())["packages"]
    engines = {
        location: package["version"]
        for location, package in packages.items()
        if not package.get("link")
        and package.get("name", location.rsplit("node_modules/", 1)[-1]) == "minimatch"
    }
    assert engines, "The lockfile no longer identifies the minimatch engines"
    vulnerable = {
        path: version for path, version in engines.items() if Version(version) < Version("11.0.1")
    }
    assert not vulnerable, f"CVE-2026-118764: {vulnerable}"
