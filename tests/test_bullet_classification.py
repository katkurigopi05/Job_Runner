"""A section is a list of entries, not a list of bullets.

Every "skip" case here is a line the rewriter was actually handed on the
owner's résumé — six project titles and six technology lines out of the
twenty-eight under Projects. Asking a model to tailor `Python, GitHub Actions`
against a job description is not a question with a good answer, and most of
what made tailored output read badly was the answers.
"""

from __future__ import annotations

import pytest

from packages.tailor.bullets import (
    is_rewritable,
    rewritable_indices,
    tailorable_bullets,
)
from packages.tailor.parse import parse_text
from packages.tailor.publish import apply_rewrites
from packages.tailor.rewrite import BulletRewrite, TailorResult

# --------------------------------------------------------------------------
# Not prose
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Attorney.AI — Citation-First Legal Research RAG Assistant   [GitHub]",
        "Project Director — AI-Native Photo/Video/Audio Editor   [GitHub]",
        "California Fire Incident Classification   [GitHub]",
        "Cloud Data Warehousing & BI Analytics   [GitHub]",
        "color-harmony — Palette Auditing Tool & Claude Skill   [GitHub]",
        "StreamBreaker AI — Music Marketing Strategist   [GitHub]",
    ],
)
def test_a_project_title_is_not_rewritten(line: str) -> None:
    assert not is_rewritable(line)


@pytest.mark.parametrize(
    "line",
    [
        "Python, FastAPI, React, HuggingFace Transformers, Qdrant",
        "Rust, TypeScript, Tauri, WebGPU, Python",
        "Python, Scikit-learn, Time-Series Forecasting",
        "Snowflake, AWS Redshift, Google BigQuery, Talend, Tableau, SQL",
        "Python, Streamlit, XGBoost",
    ],
)
def test_a_technology_line_is_not_rewritten(line: str) -> None:
    assert not is_rewritable(line)


@pytest.mark.parametrize(
    "line",
    [
        # Every one of these was handed to the model on the owner's résumé,
        # because the length fallback ran before the list test and a skills
        # line is long. `Python, GitHub Actions` came back as `Using tools, I
        # have experience with Python and GitHub Actions.` — a list rewritten
        # as a sentence, naming fewer things than it started with.
        "Languages  Python, TypeScript, Rust, C, C++, Java, JavaScript, HTML, CSS, XML, "
        "Assembly Language",
        "AI / ML & NLP  Machine Learning, Deep Learning, Generative AI (VAE, GAN, "
        "Diffusion), Transformer-based NLP (BERT, HuggingFace), RAG",
        "Frameworks & Web  FastAPI, React, Streamlit, Node.js, Tauri, WebGPU/WASM",
        "Data, DevOps & Tools  Qdrant (Vector DB), Docker, Git, GitHub Actions (CI/CD), "
        "pytest, OpenCV, Pillow, Jupyter Notebook",
        "Data Warehousing & BI  Snowflake, AWS Redshift, Google BigQuery, Talend Open "
        "Studio, Tableau, SQL",
    ],
)
def test_a_long_skills_line_is_not_rewritten(line: str) -> None:
    """A stack is a stack however long it runs.

    These are longer than `_PROSE_WORDS`, so the length test claimed them as
    prose before the list test was ever consulted. Two of them carry a category
    label — `Data, DevOps & Tools  Qdrant (Vector DB), ...` — which also hid
    them from the plain comma test, because splitting the whole line puts the
    label and the first entry into one long fragment.
    """
    assert not is_rewritable(line)


def test_a_long_prose_bullet_of_short_clauses_is_still_rewritten() -> None:
    """The counter-case to the test above, and why `_is_list` needs the period.

    Moving the list test ahead of the length test risks filing a real bullet as
    a stack. This one is three short comma-separated fragments and would match
    the list shape on that basis alone; it is prose, and the terminal full stop
    is what says so.
    """
    line = "Added model adapters, audio-upload handling, and graceful fallbacks."
    assert is_rewritable(line)


@pytest.mark.parametrize(
    "line",
    [
        "Software Engineer, Acme Corp, Jan 2021 - Present",
        "Acme Corp   2019 - 2022",
        "Master of Science in Business Analytics   Jan 2025 - Dec 2026",
    ],
)
def test_an_entry_line_with_dates_is_not_rewritten(line: str) -> None:
    assert not is_rewritable(line)


def test_a_bare_label_is_not_rewritten() -> None:
    assert not is_rewritable("ACHIEVEMENTS")
    assert not is_rewritable("Selected Work")


# --------------------------------------------------------------------------
# Prose
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Built a full-stack legal research assistant with a hybrid BM25 pipeline.",
        "Integrated specialized HuggingFace models for entity extraction.",
        "Designing a validated, reversible, replayable project-operation engine.",
        "Implemented packages for editor state, timelines, playback and export.",
        "Established TypeScript, browser E2E, Rust and linting CI across the repo.",
        "Filtered 2,338 incident records into a 1,464-row modeling dataset.",
        "Added model adapters, audio-upload handling, and graceful fallbacks.",
    ],
)
def test_a_prose_bullet_is_rewritten(line: str) -> None:
    assert is_rewritable(line)


def test_an_irregular_past_tense_opening_still_counts_as_prose() -> None:
    """`Built`, `Wrote`, `Led` carry no `-ed`, and they open most bullets."""
    assert is_rewritable("Built the ingest path")
    assert is_rewritable("Wrote the parser")


def test_an_explicit_bullet_glyph_is_always_prose() -> None:
    """The author saying which lines are bullets beats any heuristic."""
    assert is_rewritable("• Shipped it")
    assert is_rewritable("- Shipped it")


def test_a_long_line_is_prose_whatever_else_it_looks_like() -> None:
    line = "Snowflake and Redshift and BigQuery and Talend and Tableau and SQL and dbt and Spark"
    assert is_rewritable(line)


def test_one_comma_does_not_make_a_list() -> None:
    assert is_rewritable("Rebuilt the ingest path, which cut latency")


# --------------------------------------------------------------------------
# The write-back has to skip the same lines
# --------------------------------------------------------------------------


RESUME = """\
Jane Doe
jane@example.com

PROJECTS
Widget Engine   [GitHub]
Python, Rust, Postgres
Built the ingest path and cut latency in half.
Wrote the migration tool used across the team.
"""


def test_the_write_back_skips_exactly_what_the_extractor_skipped() -> None:
    """The failure this guards: every rewrite landing one line off its bullet.

    If `apply_rewrites` consumed a result for the title and the stack line, the
    first bullet's rewrite would be written onto the title, the second onto the
    stack, and the real bullets left untouched — with the diff reporting two
    changes that are both in the wrong place.
    """
    parsed = parse_text(RESUME)
    name, bullets = tailorable_bullets(parsed)

    assert name == "projects"
    assert bullets == [
        "Built the ingest path and cut latency in half.",
        "Wrote the migration tool used across the team.",
    ]

    result = TailorResult(
        bullets=[
            BulletRewrite(original=bullets[0], tailored="REWRITE ONE", changed=True),
            BulletRewrite(original=bullets[1], tailored="REWRITE TWO", changed=True),
        ]
    )
    applied = apply_rewrites(parsed, result)

    assert applied.section("projects") == [
        "Widget Engine   [GitHub]",
        "Python, Rust, Postgres",
        "REWRITE ONE",
        "REWRITE TWO",
    ]


def test_indices_and_bullets_describe_the_same_lines() -> None:
    parsed = parse_text(RESUME)
    lines = parsed.section("projects")
    _, bullets = tailorable_bullets(parsed)
    assert [lines[i] for i in rewritable_indices(lines)] == bullets


def test_a_title_is_never_overwritten_by_a_truncated_result() -> None:
    """A short result must leave the remaining bullets alone, not shift up."""
    parsed = parse_text(RESUME)
    _, bullets = tailorable_bullets(parsed)
    result = TailorResult(
        bullets=[BulletRewrite(original=bullets[0], tailored="ONLY ONE", changed=True)]
    )
    applied = apply_rewrites(parsed, result)

    assert applied.section("projects")[0] == "Widget Engine   [GitHub]"
    assert applied.section("projects")[2] == "ONLY ONE"
    assert applied.section("projects")[3] == "Wrote the migration tool used across the team."


# --------------------------------------------------------------------------
# A date range does not settle it on its own
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Led the platform migration from 2019 - 2021 and cut deploy time by half across teams",
        "Built the ingest path between Jan 2021 and Mar 2022 across four teams and two regions",
    ],
)
def test_a_bullet_that_mentions_a_span_is_still_a_bullet(line: str) -> None:
    """A date range used to force ENTRY, whatever else the line was.

    These were skipped by the rewriter and rendered with entry-name typography.
    """
    assert is_rewritable(line)


@pytest.mark.parametrize(
    "line",
    [
        "Software Engineer, Acme Corp, Jan 2021 - Present",
        "Senior Software Engineer, Platform Infrastructure Team, Acme Corp, Jan 2021 - Present",
    ],
)
def test_an_employment_header_is_still_an_entry_however_long(line: str) -> None:
    """Why the fix is the opening word and not the line length.

    The second of these is thirteen words. Making length win outright — the
    obvious fix — would have handed it to the rewriter.
    """
    assert not is_rewritable(line)


def test_a_link_marker_outranks_length() -> None:
    """The stated exception to "a long line is prose"."""
    title = (
        "Attorney.AI — Citation-First Legal Research RAG Assistant "
        "for Courts, Agencies and Regulatory Filings [GitHub]"
    )
    assert len(title.split()) > 12
    assert not is_rewritable(title)
