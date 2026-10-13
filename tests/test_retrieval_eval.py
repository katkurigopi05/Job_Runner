"""`make eval-retrieval`: where a question's own posting lands, from a clean checkout.

Every retrieval number in `docs/ML_EVALUATION.md` came from a script and a
question set under git-ignored `storage/`, so nobody but the machine that
wrote them could run them again (`docs/RAG_PIPELINE.md`, what is left, 2).
The set is in `seeds/` now and the measuring is `matching/known_target.py`.

What these hold is the arithmetic and the two ways the report could mislead:

- a posting that has closed is **not a miss**. The corpus drops postings
  after 30 days, so the set shrinks every week, and a search cannot be
  blamed for not finding what is no longer there. They are counted apart;
- the questions a rule was chosen on and the ones it was not are **never
  added together**. A number over both is a number over the tuning set.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.matching import known_target
from packages.matching.known_target import Case, Outcome, load, run, summarise
from tests.test_chat_skill_gaps import _owner, _target

SEED = Path(__file__).resolve().parent.parent / "seeds" / "retrieval_questions.json"


def outcome(rank: int | None, *, kind: str = "natural", set_name: str = "dev") -> Outcome:
    return Outcome(set=set_name, kind=kind, url="u", rank=rank, listed=30, seconds=0.5)


# --------------------------------------------------------------------------
# The arithmetic
# --------------------------------------------------------------------------


def test_a_row_counts_first_in_five_listed_and_mrr() -> None:
    rows = summarise([outcome(1), outcome(4), outcome(20), outcome(None)])

    (row,) = rows
    assert (row.set, row.kind, row.questions) == ("dev", "natural", 4)
    assert (row.first, row.in_five, row.listed) == (1, 2, 3)
    assert row.mrr == pytest.approx((1 + 1 / 4 + 1 / 20 + 0) / 4)


def test_kinds_and_sets_are_reported_apart() -> None:
    rows = summarise(
        [
            outcome(1, kind="natural", set_name="dev"),
            outcome(None, kind="paraphrased", set_name="dev"),
            outcome(2, kind="natural", set_name="heldout"),
        ]
    )

    assert [(row.set, row.kind) for row in rows] == [
        ("dev", "natural"),
        ("dev", "paraphrased"),
        ("heldout", "natural"),
    ]
    assert all(row.questions == 1 for row in rows)


def test_no_questions_is_no_row_and_not_a_zero() -> None:
    assert summarise([]) == []


# --------------------------------------------------------------------------
# The question set in the repository
# --------------------------------------------------------------------------


def test_the_tracked_set_loads_and_says_which_half_was_tuned_on() -> None:
    cases = load(SEED)
    by_set = {name: [c for c in cases if c.set == name] for name in ("dev", "heldout")}

    assert len(by_set["dev"]) >= 20 and len(by_set["heldout"]) >= 20
    assert all(case.tuned_on for case in by_set["dev"])
    assert not any(case.tuned_on for case in by_set["heldout"])
    for case in cases:
        assert case.url.startswith("https://")
        assert case.natural.strip() and case.paraphrased.strip()


def test_no_posting_is_in_both_halves() -> None:
    """A posting whose questions tuned a rule cannot also be the check on it."""
    cases = load(SEED)
    dev = {case.url for case in cases if case.set == "dev"}
    heldout = {case.url for case in cases if case.set == "heldout"}

    assert not dev & heldout


def test_a_set_with_a_question_missing_is_refused(tmp_path) -> None:
    broken = {"sets": {"dev": {"tuned_on": True, "cases": [{"url": "https://x.test/1"}]}}}
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(broken))

    with pytest.raises(ValueError, match="natural"):
        load(path)


# --------------------------------------------------------------------------
# Through the real search
# --------------------------------------------------------------------------


async def test_a_question_is_ranked_and_a_closed_posting_is_not_a_miss(worker_session) -> None:
    profile, company = await _owner(worker_session)
    stream = await _target(
        worker_session,
        profile,
        company,
        "Stream Engineer",
        "Requirements: 5+ years running Kafka in production.",
        {},
    )
    await _target(
        worker_session,
        profile,
        company,
        "Platform Engineer",
        "Requirements: Experience with Terraform and Kubernetes.",
        {},
    )
    await worker_session.commit()
    cases = [
        Case(
            set="dev",
            tuned_on=True,
            url=stream.url,
            title="Stream Engineer",
            natural="Which Stream Engineer jobs use Kafka?",
            paraphrased="jobs moving continuous flows of events between services",
        ),
        Case(
            set="dev",
            tuned_on=True,
            url="https://boards.greenhouse.io/streamco/jobs/closed-long-ago",
            title="Gone",
            natural="Which Gone jobs are there?",
            paraphrased="a job that is no longer listed",
        ),
    ]

    report = await run(worker_session, cases)

    assert (report.cases, report.present, report.gone) == (2, 1, 1)
    natural = next(o for o in report.outcomes if o.kind == "natural")
    assert natural.url == stream.url and natural.rank == 1
    # One posting present, two kinds of question: two outcomes, none for the gone one.
    assert len(report.outcomes) == 2
    assert {row.questions for row in report.rows} == {1}


async def test_the_report_carries_what_it_was_measured_on(worker_session) -> None:
    """A number with no corpus size, model or settings beside it cannot be
    compared with the next one."""
    report = await run(worker_session, [])

    stamp = report.stamp
    assert set(stamp) >= {
        "measured",
        "open_postings",
        "chunk_models",
        "semantic_candidates",
        "rerank_model",
        "search_area_applied",
    }
    assert stamp["semantic_candidates"] is False
    assert stamp["rerank_model"] in ("", None)


def test_the_measuring_asks_no_model() -> None:
    """It is `retrieve()` and arithmetic. A provider imported here would be
    a call some later change could make."""
    source = Path(known_target.__file__).read_text(encoding="utf-8")

    assert "packages.llm" not in source
