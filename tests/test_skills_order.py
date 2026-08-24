"""Front-loading the skills a posting asks for.

§2.1 permits rewriting to "rephrase, reorder, re-emphasize". Reordering is the
whole of what happens here: every skill on the source résumé is on the tailored
one, spelled exactly as the owner spelled it. Only the order changes.

Removing unrelated skills was considered and rejected. A shorter list reads
better to a human and costs you an ATS keyword scan you cannot observe — a
recruiter filtering on a skill you dropped never appears as a rejection, it
appears as silence. Reordering buys the human-readable half of that benefit
and risks nothing.
"""

from __future__ import annotations

from packages.tailor.skills import reorder_skills

JOB = (
    "Backend Engineer. You will write Go services, run PostgreSQL at scale, "
    "and deploy with Kubernetes. Familiarity with gRPC is a plus."
)


def test_asked_for_skills_come_first() -> None:
    lines = ["Java, Go, PHP, PostgreSQL"]
    assert reorder_skills(lines, JOB) == ["Go, PostgreSQL, Java, PHP"]


def test_nothing_is_added_or_removed() -> None:
    """The property that keeps this inside §2.1. Asserted as a set so a
    reordering bug cannot pass by dropping the awkward item."""
    lines = ["Java, Go, PHP, PostgreSQL, COBOL"]
    before = {s.strip() for s in lines[0].split(",")}
    after = {s.strip() for s in reorder_skills(lines, JOB)[0].split(",")}
    assert before == after


def test_a_label_stays_at_the_front_of_its_line() -> None:
    """ "Languages: Go, Java" must not become "Go, Java, Languages:"."""
    lines = ["Languages: Java, Go, PHP"]
    assert reorder_skills(lines, JOB) == ["Languages: Go, Java, PHP"]


def test_line_order_is_preserved() -> None:
    """Labelled groups carry meaning. Sorting whole lines by relevance would
    put "Tools" above "Languages" and quietly restructure the résumé."""
    lines = ["Languages: Java, Go", "Databases: MySQL, PostgreSQL"]
    assert reorder_skills(lines, JOB) == [
        "Languages: Go, Java",
        "Databases: PostgreSQL, MySQL",
    ]


def test_relative_order_within_each_group_is_kept() -> None:
    """Stable partition, not a sort. The owner's ordering is information —
    they listed their strongest first — and it survives among the skills the
    posting did not ask for."""
    lines = ["Rust, Java, Go, Zig, PostgreSQL"]
    assert reorder_skills(lines, JOB) == ["Go, PostgreSQL, Rust, Java, Zig"]


def test_a_line_with_no_separators_is_returned_unchanged() -> None:
    lines = ["Comfortable across the stack"]
    assert reorder_skills(lines, JOB) == ["Comfortable across the stack"]


def test_matching_ignores_case_and_punctuation() -> None:
    lines = ["postgresql, java, GO"]
    assert reorder_skills(lines, JOB)[0].startswith("postgresql")


def test_a_multi_word_skill_matches_on_its_words() -> None:
    lines = ["Machine Learning, Kubernetes Operators, Java"]
    assert reorder_skills(lines, JOB)[0].startswith("Kubernetes Operators")


def test_no_posting_text_changes_nothing() -> None:
    lines = ["Java, Go, PHP"]
    assert reorder_skills(lines, "") == lines


def test_empty_input_is_not_an_error() -> None:
    assert reorder_skills([], JOB) == []
    assert reorder_skills([""], JOB) == [""]
