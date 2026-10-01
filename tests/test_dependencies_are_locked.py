"""CI installs exact versions, and those versions still satisfy pyproject.toml.

On 2026-09-28 main went red with no code change: CI installed unpinned, picked
up SQLAlchemy 2.1.1, and mypy failed the gate for every pull request. The fix
is constraints.txt, an exact lock CI, the image and `make install` all read.

A lock only helps while it agrees with what it locks. These hold the two ways
it stops agreeing without anything else failing: a dependency added to
pyproject.toml without `make lock`, so CI installs it unpinned again; and a
range tightened in pyproject.toml that the old pin no longer satisfies, so pip
refuses to install at all.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
CONSTRAINTS = (ROOT / "constraints.txt").read_text()

PINS: dict[str, list[str]] = {}
for line in CONSTRAINTS.splitlines():
    match = re.match(r"^([A-Za-z0-9._-]+)==([^\s;]+)", line)
    if match:
        PINS.setdefault(canonicalize_name(match[1]), []).append(match[2])

DIRECT = [
    Requirement(spec)
    for spec in PYPROJECT["project"]["dependencies"]
    + PYPROJECT["project"]["optional-dependencies"]["dev"]
]


@pytest.mark.parametrize("requirement", DIRECT, ids=lambda r: r.name)
def test_every_direct_dependency_is_pinned(requirement: Requirement) -> None:
    assert canonicalize_name(requirement.name) in PINS, (
        f"{requirement.name} is in pyproject.toml but not in constraints.txt, so CI "
        "installs whatever is newest again. Run `make lock` and commit the result."
    )


@pytest.mark.parametrize("requirement", DIRECT, ids=lambda r: r.name)
def test_every_pin_satisfies_pyproject(requirement: Requirement) -> None:
    for version in PINS.get(canonicalize_name(requirement.name), []):
        assert requirement.specifier.contains(version, prereleases=True), (
            f"constraints.txt pins {requirement.name}=={version}, outside "
            f"pyproject.toml's {requirement.specifier}. Run `make lock`."
        )


def test_ci_installs_from_the_lock() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert '.venv/bin/pip install -e ".[dev]" -c constraints.txt' in workflow


def test_the_image_installs_from_the_lock() -> None:
    """Both installs: the second would otherwise upgrade what the first pinned."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    installs = re.findall(r'pip install -e "\.\[dev\]"[^\n]*', dockerfile)
    assert installs, "the Dockerfile no longer installs the project"
    assert all("-c constraints.txt" in line for line in installs), installs
    assert "constraints.txt" in re.search(r"^COPY [^\n]*pyproject\.toml[^\n]*", dockerfile, re.M)[0]
