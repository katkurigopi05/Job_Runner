"""The setup instructions have to name commands that exist, in a usable order.

The defect that prompted this was small and total: the README's first-run
sequence went `make api`, `make worker`, `make web` — and `make web` runs
`npm run dev` in a directory whose `node_modules` nothing had created. A first
run failed on a missing `next` binary, which names the tool rather than the
missing step.

These are prose tests, and prose is where this repo keeps being wrong: §15
records a CSV row count that went stale twice, and the dashboard's bind address
was documented as loopback for as long as it was not. A command list is
checkable against the Makefile, so it is checked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
USAGE = (ROOT / "docs/USAGE.md").read_text(encoding="utf-8")
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")

#: Targets the Makefile actually defines.
TARGETS = set(re.findall(r"^([a-z][a-z0-9-]*):", MAKEFILE, re.MULTILINE))

#: `make <target>` as the documents invoke it, variables stripped.
_MAKE_CALL = re.compile(r"\bmake\s+([a-z][a-z0-9-]*)")

#: Fenced blocks and inline code spans. Prose is excluded deliberately: the
#: first draft read the whole document and reported `make it` and `make this`
#: as undefined targets, which is a test failing on English.
_FENCED = re.compile(r"```[a-z]*\n(.*?)```", re.S)
_INLINE = re.compile(r"`([^`\n]+)`")


def _invoked(document: str) -> set[str]:
    code = _FENCED.findall(document) + _INLINE.findall(document)
    return {target for snippet in code for target in _MAKE_CALL.findall(snippet)}


def test_there_are_targets_to_check_against() -> None:
    """A regex that matched nothing would make every test below vacuous."""
    assert {"install", "up", "migrate", "web", "web-install", "doctor"} <= TARGETS


#: Keyed by name so a failure reports the file rather than the file's contents.
DOCUMENTS = {"README.md": README, "docs/USAGE.md": USAGE}


@pytest.mark.parametrize("name", sorted(DOCUMENTS))
def test_every_documented_make_target_exists(name: str) -> None:
    missing = sorted(_invoked(DOCUMENTS[name]) - TARGETS)
    assert not missing, (
        f"{name} tells the reader to run targets the Makefile does not define: {missing}"
    )


def test_the_readme_installs_the_dashboard_before_starting_it() -> None:
    """The ordering *is* the fix. Both commands being present is not enough."""
    install_at = README.find("make web-install")
    # `make web` is followed by an aligned comment in the block, so an exact
    # "make web\n" never matched and the ordering assertion below could not run.
    started = re.search(r"^make web(?:\s|$)", README, re.MULTILINE)
    start_at = started.start() if started else -1
    assert install_at != -1, "the README never tells the reader to install the dashboard"
    assert start_at != -1, "the README never tells the reader to start the dashboard"
    assert install_at < start_at, "make web comes before make web-install"


@pytest.mark.parametrize(
    "command",
    [
        "playwright install chromium",
        "make nltk-data",
        "make doctor",
    ],
)
def test_the_readme_names_the_dependencies_pip_cannot_install(command: str) -> None:
    """Three things `make install` does not do, each failing differently.

    The browser fails loudly, Pango segfaults pytest partway through a run, and
    the tagger data does not fail at all — the fabrication guard silently drops
    to a weaker check. The last is the one a reader has to be told about,
    because nothing will tell them later.
    """
    assert command in README, f"the README does not mention {command!r}"


def test_both_documents_agree_about_the_setup_steps() -> None:
    """One of them being right is how the other one goes stale unnoticed."""
    for command in ("make web-install", "make nltk-data", "playwright install chromium"):
        assert command in README, command
        assert command in USAGE, command


def test_pango_is_documented_for_both_platforms() -> None:
    """WeasyPrint is a hard dependency of the résumé PDF, and a missing Pango
    does not raise — it takes the interpreter down mid-test."""
    assert "brew install pango" in README
    assert "libpango" in README, "the Debian/Ubuntu package name is not given"
