"""A tool's name is not evidence. A bullet that says what the tool was used for is.

`practices.py` first read "automated testing" into any bullet that named one
of thirteen test runners. Two things were wrong with that, and the owner
named both on 2026-10-13:

- **Thirteen tools on one argument.** The argument was made for pytest. The
  other twelve were listed because they are the same kind of thing, which is
  how a table of "related terms" starts (`aliases.py`). The row is pytest.
- **A name is a mention.** "Skills: pytest" lists it. "Planning to learn
  pytest" hopes for it. "Did not use pytest" denies it. None says the owner
  tested anything, and each named the tool.

So the evidence is a statement, in the bullet itself, that pytest was used
for tests or checks. And a revision that is kept still says so: pytest is
there, the practice stands beside it, and neither is denied.

Offline, like `tests/test_targeted_tailoring.py`, whose fixtures these are.
"""

from __future__ import annotations

import pytest

from packages.tailor.guard import SourceCorpus
from packages.tailor.practices import PRACTICES, affirmed_in, stands_beside
from packages.tailor.targeted import NO_GAIN, Status, Target, classify, judge, revise
from tests.test_targeted_tailoring import (
    ACCEPTED,
    ADDS_A_GAP,
    CLASSIFIER,
    DROPS_THE_COUNT,
    DROPS_THE_TOOL,
    HANDBOOK,
    LOADER,
    POSTING,
    RESUME,
    Scripted,
    by_term,
    resume,
)

TERM = "automated testing"


# --------------------------------------------------------------------------
# What counts as having said it
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bullet",
    [
        "Loaded 1,000 records with pytest checks.",
        LOADER,
        "Covered the billing loader with pytest tests.",
        "Tested every warehouse load with pytest.",
        "Wrote a pytest suite for the billing exports.",
    ],
)
def test_a_bullet_that_says_pytest_ran_the_checks_permits_the_practice(bullet: str) -> None:
    assert affirmed_in(TERM, bullet) == "pytest"


@pytest.mark.parametrize(
    "line",
    [
        # A list. It names the tool and says nothing was done with it.
        "Skills: pytest",
        "Languages & Tools  Python, pytest, pandas",
        "Testing  pytest, Playwright",
        # A mention, with no test or check anywhere near it.
        "Built the billing loader in Python and pytest.",
        # Something hoped for.
        "Planning to learn pytest.",
        "Planning to write pytest tests for the loader next quarter.",
        "Currently learning pytest for future data checks.",
        # Something denied.
        "Did not use pytest.",
        "Shipped the loader without pytest checks.",
        "Loaded 1,000 records, though pytest checks were never written.",
        "Replaced pytest checks with a manual review.",
    ],
)
def test_a_list_a_mention_an_aspiration_and_a_negation_do_not(line: str) -> None:
    assert affirmed_in(TERM, line) is None


def test_the_row_is_pytest_and_nothing_else() -> None:
    assert [sorted(practice.tools) for practice in PRACTICES] == [["pytest"]]


@pytest.mark.parametrize(
    "bullet",
    [
        "Covered the checkout flow with Jest tests.",
        "Wrote JUnit tests for the billing service.",
        "Ran Playwright checks on every release.",
        "Kept a Selenium suite for the admin pages.",
        "Wrote unittest checks for the parser.",
    ],
)
def test_another_test_runner_does_not_activate_it(bullet: str) -> None:
    parsed = resume([CLASSIFIER, bullet, HANDBOOK])

    plan = classify(parsed, POSTING)

    assert affirmed_in(TERM, bullet) is None
    assert by_term(plan)[TERM].status is Status.GAP
    assert plan.targets == {}


# --------------------------------------------------------------------------
# What that does to a résumé
# --------------------------------------------------------------------------


def test_the_evidence_is_the_bullet_and_the_skills_line_is_not_a_source() -> None:
    testing = by_term(classify(RESUME, POSTING))[TERM]

    assert testing.status is Status.SUPPORTED
    assert testing.sources == ("experience:1",) and testing.bullet == 1


@pytest.mark.parametrize(
    "bullet",
    [
        "Loaded 1,000 billing records into the warehouse.",  # pytest only in Skills
        "Planning to learn pytest for the billing loader.",
        "Did not use pytest on the legacy loader.",
        "Built the billing loader in Python and pytest.",
    ],
)
async def test_without_a_statement_the_term_is_a_gap_and_nothing_is_asked(bullet: str) -> None:
    parsed = resume(
        [CLASSIFIER, bullet, HANDBOOK], skills=["Languages & Tools  Python, pytest, pandas"]
    )
    provider = Scripted(ACCEPTED)

    plan = classify(parsed, POSTING)
    revision = await revise(provider, parsed, POSTING)

    testing = by_term(plan)[TERM]
    assert testing.status is Status.GAP and testing.sources == () and testing.bullet is None
    assert TERM in plan.gaps
    assert provider.calls == [] and revision.message == NO_GAIN


async def test_another_projects_statement_does_not_authorize_an_employers_claim() -> None:
    """The project's bullet says it, in full. The bullets being tailored are
    the employer's, and none of them does."""
    parsed = resume(
        ["Built billing exports in Python for the finance team.", HANDBOOK],
        projects=[
            "Ledger Checker — Side Project   [GitHub]",
            "Python, pytest",
            "Verified 1,000 ledger rows with pytest checks.",
        ],
    )
    greedy = Scripted("Built billing exports in Python with automated testing in pytest.")

    plan = classify(parsed, POSTING)
    revision = await revise(greedy, parsed, POSTING)

    testing = by_term(plan)[TERM]
    assert testing.status is Status.SUPPORTED
    # The bullet, not the stack line above it that only lists the tool.
    assert testing.sources == ("projects:2",)
    assert testing.bullet is None and plan.targets == {}
    assert greedy.calls == [] and revision.message == NO_GAIN


# --------------------------------------------------------------------------
# What a kept revision still has to say
# --------------------------------------------------------------------------

TARGET = (Target(term=TERM, evidence="pytest", practice=True),)
ACROSS_THE_COMPANY = (
    "Loaded 1,000 billing records across the company with automated testing via pytest checks."
)


def test_the_practice_stands_beside_its_tool() -> None:
    assert stands_beside(TERM, "pytest", ACCEPTED)
    assert stands_beside(
        TERM, "pytest", "Loaded 1,000 records with pytest checks for automated testing."
    )
    assert not stands_beside(
        TERM, "pytest", "Led automated testing and loaded 1,000 billing records with pytest checks."
    )
    assert not stands_beside(
        TERM, "pytest", "Loaded 1,000 records without automated testing in pytest."
    )


@pytest.mark.parametrize(
    "answer, why",
    [
        # Every word kept and none added but "and": the practice is now a
        # second claim next to the checks, not a name for them.
        ("Loaded 1,000 billing records with pytest checks and automated testing.", "beside"),
        ("Automated testing: loaded 1,000 billing records with pytest checks.", "beside"),
        # Every word kept and two of them moved. The recombination check has
        # always refused that, and still does.
        ("Checks: loaded 1,000 billing records with automated testing in pytest.", "combines"),
        # The practice denied. "not" is a word the revision was not asked for.
        ("Loaded 1,000 billing records with pytest checks, not automated testing.", "'not'"),
        ("Loaded 1,000 billing records without automated testing in pytest checks.", "'without'"),
    ],
)
def test_a_revision_that_moves_or_denies_the_claim_is_not_kept(answer: str, why: str) -> None:
    corpus = SourceCorpus.from_resume(RESUME)

    added, reason, _, _ = judge(LOADER, answer, TARGET, corpus, ("Kubernetes",))

    assert added == ()
    assert reason is not None and why in reason, reason


@pytest.mark.parametrize(
    "answer, why",
    [
        (DROPS_THE_COUNT, "'1,000'"),  # a number
        (DROPS_THE_TOOL, "'pytest'"),  # a technology
        (ADDS_A_GAP, "'Kubernetes'"),  # an unsupported addition
        # Another, in words the fabrication guard reads as ordinary English.
        (ACROSS_THE_COMPANY, "'company'"),
    ],
)
async def test_what_was_refused_before_is_still_refused(answer: str, why: str) -> None:
    revision = await revise(Scripted(answer), RESUME, POSTING)

    loader = revision.result.bullets[1]
    assert loader.tailored == LOADER and not loader.changed
    assert loader.rejected_reason is not None and why in loader.rejected_reason, loader


async def test_a_changed_unit_is_still_refused() -> None:
    timed = "Loaded 1,000 billing records in 40 seconds with pytest checks."
    parsed = resume([CLASSIFIER, timed, HANDBOOK])
    slower = "Loaded 1,000 billing records in 40 minutes with automated testing via pytest checks."

    revision = await revise(Scripted(slower), parsed, POSTING)

    assert revision.result.bullets[1].tailored == timed
    assert revision.result.bullets[1].rejected_reason is not None


async def test_the_revision_it_was_built_for_is_still_kept() -> None:
    revision = await revise(Scripted(ACCEPTED), RESUME, POSTING)

    assert revision.result.bullets[1].tailored == ACCEPTED
    assert revision.gained == (TERM,)


def test_only_the_terms_asked_for_may_arrive() -> None:
    """ "Across the company" is ordinary English to the fabrication guard,
    and it is a larger claim than the bullet made."""
    from packages.tailor.targeted import brought_in

    assert brought_in(LOADER, ACCEPTED, TARGET) == ()
    assert brought_in(LOADER, ACROSS_THE_COMPANY, TARGET) == ("company",)
    assert brought_in(LOADER, ACCEPTED.replace("Loaded", "Reliably loaded"), TARGET) == (
        "Reliably",
    )
