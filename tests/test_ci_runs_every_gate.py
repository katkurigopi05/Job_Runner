"""CI must name every gate, and mean the same thing by it.

CLAUDE.md §13 warns about one half of this: a `gate-N` in the Makefile but not
in `ci.yml` still *runs* — gate 0 covers every test — but a failure in it
reports as a gate-0 failure and you lose the label saying which phase
regressed.

The other half is what actually happened, and it is harder to see because the
CI step is right there wearing the correct name. CI repeated the file lists
instead of reading the Makefile's, and they drifted:

    gate-2   CI ran   4 tests, make gate-2 ran  18
    gate-3   CI ran 131 tests, make gate-3 ran 178
    gate-5   CI ran 150 tests, make gate-5 ran 243

Nothing went unchecked — gate-0 runs the whole suite — but "gate-3 is green"
in CI stopped meaning what `make gate-3` means, which is precisely the label
§13 says the named steps exist to provide.

The lists now live once, in `GATEn_TESTS`, and CI runs `make gate-N-only`.
These tests hold the two properties that survive that fix: every gate is named
in CI, and no CI step goes back to spelling out its own file list.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = (ROOT / "Makefile").read_text()
WORKFLOW = (ROOT / ".github/workflows/ci.yml").read_text()

#: `gate-1`, `gate-3` … but not `gate-2-live`, which needs a real posting, nor
#: the `-only` twins, which are the same gate without the gate-0 dependency.
GATES = sorted(
    name
    for name in re.findall(r"^(gate-\d+):", MAKEFILE, re.MULTILINE)
    if not name.endswith(("-live", "-only"))
)


def test_there_are_gates_to_check() -> None:
    """A regex that matched nothing would make every test below vacuous."""
    assert len(GATES) >= 7, GATES


@pytest.mark.parametrize("gate", GATES)
def test_ci_runs_every_gate_by_name(gate: str) -> None:
    """A gate CI never names loses its label the moment it fails."""
    assert f"make {gate}\n" in WORKFLOW or f"make {gate}-only" in WORKFLOW, (
        f"{gate} exists in the Makefile but ci.yml never runs it; a failure in "
        f"it will report as gate-0 and you will not know which phase regressed"
    )


@pytest.mark.parametrize("gate", [g for g in GATES if g != "gate-0"])
def test_ci_does_not_keep_its_own_copy_of_a_gates_test_list(gate: str) -> None:
    """The drift itself: two lists for one gate is how they disagree.

    A CI step that spells out `pytest -q tests/...` is a second definition, and
    a test added to the Makefile's gate never reaches it.
    """
    number = gate.rsplit("-", 1)[1]
    step = re.search(rf"- name: gate-{number}[^\n]*\n(?:\s+#[^\n]*\n)*\s+run: ([^\n]+)", WORKFLOW)
    assert step is not None, f"no ci.yml step named gate-{number}"
    assert "tests/" not in step.group(1), (
        f"the gate-{number} step names test files itself: {step.group(1)!r}. "
        f"Run `make gate-{number}-only` so GATE{number}_TESTS stays the one list."
    )
