"""§2.7 and §2.8, held at the one file both rest on.

`.env` "is gitignored and stays gitignored" (§2.7), and résumés are local
storage only (§2.8). `.gitignore` is what makes either true, and nothing held
it. A rule deleted in a tidy-up, or re-included by a `!` line written for
something else, makes the next `git add -A` commit a key, a résumé or a
browser profile with a live ATS session in it, and no gate goes red.

The idea is `tools/security_guards.py` in MadsLorentzen/ai-job-search (MIT),
which holds its template's ignore rules for the same reason. Its other two
checks guard pre-approved permissions and install scripts shipped to every
fork, which this repository does not have.

Asked of git itself and not read out of the file, so a negation, a nested
`.gitignore` or a rule in the wrong order is seen as git would see it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: A path of each kind that must never be committed, and what it holds.
PRIVATE = {
    ".env": "provider keys and the mailbox password (§2.7)",
    ".env.local": "the same, under the other name tools write",
    ".secrets/vault.key": "the vault's key (§2.7)",
    ".secrets/llm-audit.jsonl": "the record of what left the machine (§2.8)",
    "storage/resumes/candidate/v1/resume.pdf": "a résumé (§2.8)",
    "storage/browser/greenhouse/Default/Cookies": "a signed-in ATS session",
    "storage/receipts/application.png": "a screenshot of a filled form",
    "storage/backups/jobrunner.dump": "a copy of the database",
    "storage/logs/worker.log": "the worker's log",
}

#: What may be tracked under those roots: the template a checkout copies.
TRACKED_ON_PURPOSE = {".env.example"}


def git(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments], cwd=ROOT, capture_output=True, text=True, check=False
    )


@pytest.fixture(scope="module", autouse=True)
def _a_git_checkout() -> None:
    try:
        inside = git("rev-parse", "--is-inside-work-tree")
    except FileNotFoundError:
        pytest.skip("git is not installed")
    if inside.returncode != 0:
        pytest.skip("not a git checkout (an image, or an exported tree)")


@pytest.mark.parametrize("path", sorted(PRIVATE))
def test_git_would_not_add_it(path: str) -> None:
    # `--no-index`: the rules alone. Without it a file somebody had already
    # committed would be reported as not ignored for that reason instead.
    ignored = git("check-ignore", "--quiet", "--no-index", path)

    assert ignored.returncode == 0, f"{path} is not ignored: {PRIVATE[path]}"


def test_the_question_can_be_answered_no() -> None:
    """Or every test above would pass against a `.gitignore` of `*`."""
    assert git("check-ignore", "--quiet", "--no-index", "README.md").returncode == 1
    assert git("check-ignore", "--quiet", "--no-index", ".env.example").returncode == 1


def test_nothing_under_those_roots_is_tracked() -> None:
    """A rule added after the file was committed ignores nothing."""
    tracked = git("ls-files", "--", ".env", ".env.*", ".secrets", "storage")

    assert tracked.returncode == 0
    assert set(tracked.stdout.split()) == TRACKED_ON_PURPOSE
