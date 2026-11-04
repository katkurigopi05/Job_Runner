"""The 500 paraphrased questions: reading the file and scoring what came back.

`docs/evaluations/paraphrased_questions_500.md` is written for people and for
agents, so the scorer reads that file rather than a second copy that could
drift from it. These tests hold the reading and the arithmetic; the search
itself is `retrieve()`, which has its own tests.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from packages.matching import paraphrased_set as P

A, B, C = (str(uuid.UUID(int=n)) for n in (1, 2, 3))

HEADER = (
    "| # | Question | Target posting | Where | Posting id | Twins |\n|---:|---|---|---|---|---|"
)


def _row(*cells: str) -> str:
    return "| " + " | ".join(cells) + " |"


SAMPLE = "\n".join(
    [
        "# Title",
        "",
        "## Software engineering (2)",
        "",
        HEADER,
        _row(
            "1",
            "Roles building payment rails",
            "[Engineer \\| Payments — Acme](https://example.test/1)",
            "San Francisco \\| Remote",
            f"`{A}`",
            "#3",
        ),
        _row(
            "2",
            "Jobs keeping storage fast",
            "[Storage Engineer — Beta](https://example.test/2)",
            "Remote",
            f"`{B}`",
            "",
        ),
        "",
        "## Sales and partnerships (1)",
        "",
        HEADER,
        _row(
            "3",
            "Field roles selling payment rails",
            "[Engineer, Payments — Acme](https://example.test/3)",
            "Austin, TX",
            f"`{C}`",
            "#1",
        ),
        "",
        "## Appendix A: company-name traps (1)",
        "",
        "| # | Original wording | Read as naming |",
        "|---:|---|---|",
        "| 2 | Jobs keeping storage fast at scale | Scale AI |",
        "",
        "## Appendix B: words read as a company with nothing open",
        "",
        "| # | Company read |",
        "|---:|---|",
        "| 1 | NEAR |",
        "",
    ]
)


def _write(tmp_path: Path, text: str = SAMPLE) -> Path:
    path = tmp_path / "set.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_rows_are_read_with_their_family_target_and_twins(tmp_path):
    cases, traps = P.load(_write(tmp_path))

    assert [c.number for c in cases] == [1, 2, 3]
    first = cases[0]
    assert first.family == "Software engineering"
    assert first.question == "Roles building payment rails"
    assert first.target == "Engineer | Payments — Acme"  # an escaped pipe is not a column
    assert first.url == "https://example.test/1"
    assert first.posting_id == uuid.UUID(A)
    assert first.twins == (3,)
    assert cases[1].twins == ()
    assert cases[2].family == "Sales and partnerships"


def test_a_trap_asks_its_original_wording_for_the_same_target(tmp_path):
    cases, traps = P.load(_write(tmp_path))

    [trap] = traps
    assert trap.number == 2 and trap.trap == "Scale AI"
    assert trap.question == "Jobs keeping storage fast at scale"
    assert trap.posting_id == cases[1].posting_id and trap.family == cases[1].family
    assert not any(c.trap for c in cases)


def test_appendix_rows_are_never_read_as_questions(tmp_path):
    cases, _ = P.load(_write(tmp_path))

    assert all(c.question != "NEAR" for c in cases) and len(cases) == 3


def test_a_number_used_twice_is_refused(tmp_path):
    doubled = SAMPLE.replace("| 2 | Jobs keeping", "| 1 | Jobs keeping")

    with pytest.raises(ValueError, match="twice"):
        P.load(_write(tmp_path, doubled))


def test_a_twin_that_is_not_in_the_set_is_refused(tmp_path):
    with pytest.raises(ValueError, match="twin"):
        P.load(_write(tmp_path, SAMPLE.replace("| #3 |", "| #9 |")))


def test_the_rank_counts_the_five_shown_then_the_list():
    target, other, twin = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    assert P.rank_of([other, target], target) == 2
    assert P.rank_of([other], target) is None
    assert P.best_rank([other, twin, target], [target, twin]) == 2


def _outcome(n, rank, twin_rank=None, family="Software engineering", seconds=1.0):
    return P.Outcome(
        number=n,
        family=family,
        trap=None,
        rank=rank,
        twin_rank=rank if twin_rank is None else twin_rank,
        listed=40,
        seconds=seconds,
    )


def test_a_row_scores_strict_and_with_twins_apart():
    outcomes = [
        _outcome(1, 1),
        _outcome(2, 7, twin_rank=3),
        _outcome(3, None),
        _outcome(4, 18, seconds=3.0),
    ]

    [row] = P.summarise(outcomes, group="all")

    assert (row.questions, row.first, row.in_five, row.in_twenty, row.listed) == (4, 1, 1, 3, 3)
    assert row.mrr == pytest.approx((1 + 1 / 7 + 1 / 18) / 4)
    assert row.in_five_twins == 2
    assert row.mrr_twins == pytest.approx((1 + 1 / 3 + 1 / 18) / 4)
    assert row.median_seconds == pytest.approx(1.0)


def test_families_are_rows_of_their_own_and_never_mixed_with_traps():
    outcomes = [
        _outcome(1, 1),
        _outcome(2, None, family="Sales and partnerships"),
        P.Outcome(2, "Sales and partnerships", "Scale AI", None, None, 0, 1.0),
    ]

    rows = P.rows_for(outcomes)

    by_group = {row.group: row for row in rows}
    assert by_group["all"].questions == 2
    assert by_group["Sales and partnerships"].questions == 1
    assert by_group["traps (original wording)"].questions == 1


def test_the_tracked_set_has_500_questions_and_15_traps():
    cases, traps = P.load(P.DEFAULT_SET)

    assert [c.number for c in cases] == list(range(1, 501))
    assert len({c.posting_id for c in cases}) == 500
    assert len(traps) == 15


def test_a_question_whose_search_raised_is_an_error_not_a_miss():
    failed = P.Outcome(5, "Software engineering", None, None, None, 0, 0.2, error="InterfaceError")

    [row] = P.summarise([_outcome(1, 1), failed], group="all")

    assert row.errors == 1
    assert row.questions == 1 and row.in_five == 1 and row.mrr == 1.0
