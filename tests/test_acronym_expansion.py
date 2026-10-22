"""An acronym the bullet already uses is written out by code, not by a model.

Targeted tailoring on ten real postings (2026-10-21) found three terms the
résumé backs under another spelling. Two were plain acronyms: "UI" for a
required "user interface" and "LLM" for "large language models". The local
model was asked to add them and failed both times it was used: once it added
"supporting", which a targeted revision may not, and once it wrote "and large
language models (LLM) marketing strategist". Writing `UI (user interface)`
needs no model. The bullet already states the fact; this only spells it.

Only an acronym whose letters begin the term's words is expanded. "ETL" is
an alias of "data pipelines" but not its abbreviation, so that one still goes
to the model. Every expansion is held to `judge`, exactly as a model's
revision is.
"""

from __future__ import annotations

import pytest

from packages.llm.provider import StubProvider
from packages.tailor.targeted import Target, classify, expand_acronyms, revise
from tests.test_targeted_tailoring import resume

UI = Target(term="user interface", evidence="UI")
LLM = Target(term="large language models", evidence="LLM")
APIS = Target(term="application programming interface", evidence="APIs")
ETL = Target(term="data pipelines", evidence="ETL")


@pytest.mark.parametrize(
    "bullet,targets,expected",
    [
        (
            "Built the Streamlit UI for a reporting tool.",
            [UI],
            "Built the Streamlit UI (user interface) for a reporting tool.",
        ),
        (
            # The bullet says one model, so the term is written in the singular.
            "Wired an LLM into the help desk.",
            [LLM],
            "Wired an LLM (large language model) into the help desk.",
        ),
        (
            "Called internal APIs from the worker.",
            [APIS],
            "Called internal APIs (application programming interface) from the worker.",
        ),
        (
            "Built the UI and wired an LLM behind it.",
            [UI, LLM],
            "Built the UI (user interface) and wired an LLM (large language model) behind it.",
        ),
    ],
)
def test_an_acronym_is_written_out_beside_itself(
    bullet: str, targets: list[Target], expected: str
) -> None:
    assert expand_acronyms(bullet, targets) == expected


@pytest.mark.parametrize(
    "bullet,targets",
    [
        # An alias, not an abbreviation: its letters do not spell the term.
        ("Built Talend ETL jobs over order data.", [ETL]),
        # Inside another word.
        ("Built a GUI for the scanner.", [UI]),
        # Already followed by brackets; a second pair would read badly.
        ("Wired an LLM (Ollama) into the help desk.", [LLM]),
        # Not in the bullet at all.
        ("Built a reporting tool.", [UI]),
        # All or nothing: one target the code cannot write leaves it to the model.
        ("Built the UI over Talend ETL jobs.", [UI, ETL]),
        # A practice is a claim about what a tool did, not a spelling.
        ("Ran pytest checks on UI code.", [Target("user interface", "UI", practice=True)]),
    ],
)
def test_anything_else_is_left_to_the_model(bullet: str, targets: list[Target]) -> None:
    assert expand_acronyms(bullet, targets) is None


class Refuses(StubProvider):
    """A provider that fails the test if anything asks it."""

    async def complete(self, system, user, *, max_tokens=1024, temperature=0.2) -> str:  # noqa: ANN001
        raise AssertionError("no model should be asked to write out an acronym")


async def test_revise_expands_an_acronym_without_a_model() -> None:
    parsed = resume(
        ["Built the Streamlit UI for the reporting tool."],
        skills=["Tools  Python, Streamlit"],
    )
    posting = "Requirements:\nUser interface development with Python.\n"
    plan = classify(parsed, posting)
    assert plan.targets, "the plan should target the UI bullet"

    revision = await revise(Refuses(), parsed, posting, plan=plan)

    assert revision.calls == 0
    assert revision.gained == ("user interface",)
    [rewrite] = [b for b in revision.result.bullets if b.changed]
    assert rewrite.tailored == "Built the Streamlit UI (user interface) for the reporting tool."


class Scripted(StubProvider):
    def __init__(self, answer: str) -> None:
        super().__init__()
        self.answer = answer
        self.calls = 0

    async def complete(self, system, user, *, max_tokens=1024, temperature=0.2) -> str:  # noqa: ANN001
        self.calls += 1
        return self.answer


async def test_a_synonym_that_is_not_an_acronym_still_asks_the_model() -> None:
    bullet = "Built Talend ETL jobs to join order sources."
    parsed = resume([bullet], skills=["Tools  Talend, SQL"])
    posting = "Requirements:\nData pipelines.\n"
    plan = classify(parsed, posting)
    assert plan.targets, "the plan should target the ETL bullet"

    provider = Scripted("Built Talend ETL data pipelines to join order sources.")
    revision = await revise(provider, parsed, posting, plan=plan)

    assert provider.calls >= 1
    assert revision.calls == provider.calls
