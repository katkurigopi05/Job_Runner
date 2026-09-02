"""A rewrite may not quietly delete a technology the résumé lists.

Every check in `guard.py` reads the output and asks whether it says something
the source does not. None of them reads what is *missing*, so a rewrite that
removes a library passes cleanly — nothing was invented — and the employer
receives a document with fewer of the keywords an ATS scans for.

Both models tested on the owner's résumé did this, on the same bullet and in
the same place:

    Built a companion Python application (Pillow/Tkinter) with 35
    non-destructive editing operations, ...

llama3.1 returned it without `(Pillow/Tkinter)` and without `companion`;
the cloud model returned it without `(Pillow/Tkinter)`. Neither was refused,
because deleting a true thing is not fabrication.
"""

from __future__ import annotations

import pytest

from packages.tailor.guard import SourceCorpus
from packages.tailor.parse import parse_text
from packages.tailor.rewrite import vet
from packages.tailor.technologies import dropped, inventory

RESUME = """Projects
Project Director — AI-Native Photo/Video/Audio Editor   [GitHub]
Rust, TypeScript, Tauri, WebGPU, Python
Built a companion Python application (Pillow/Tkinter) with 35 non-destructive editing operations.
Established TypeScript, browser E2E, Rust, linting, and type-checking CI across 200 files.
Skills
Data, DevOps & Tools  Qdrant (Vector DB), Docker, Git, GitHub Actions (CI/CD), pytest, Pillow
"""

BULLET = (
    "Built a companion Python application (Pillow/Tkinter) with 35 "
    "non-destructive editing operations."
)


@pytest.fixture
def corpus() -> SourceCorpus:
    return SourceCorpus.from_resume(parse_text(RESUME))


# --------------------------------------------------------------------------
# The inventory
# --------------------------------------------------------------------------


def test_the_inventory_reads_the_stack_and_skills_lines() -> None:
    listed = inventory(parse_text(RESUME))
    for term in ("pillow", "qdrant", "pytest", "docker", "tauri", "webgpu", "python"):
        assert term in listed, term


def test_a_compound_is_indexed_by_its_parts() -> None:
    """`GitHub Actions (CI/CD)` lists CI and CD, not `ci/cd`.

    A rewrite drops these one at a time, so each has to be protected on its
    own or the check never fires.
    """
    listed = inventory(parse_text(RESUME))
    assert "ci" in listed
    assert "cd" in listed


def test_a_bare_letter_is_not_protected() -> None:
    """`C` and `R` are real languages and also initials and list markers.

    Protecting them costs refused rewrites for no gain, so the inventory keeps
    a minimum length.
    """
    listed = inventory(parse_text("Skills\nLanguages  C, R, Python, Rust, Java\n"))
    assert "c" not in listed
    assert "r" not in listed
    assert "python" in listed


# --------------------------------------------------------------------------
# The refusal
# --------------------------------------------------------------------------


def test_dropping_a_listed_library_is_refused(corpus: SourceCorpus) -> None:
    candidate = "Built a companion Python application with 35 non-destructive editing operations."
    accepted, reason, _report = vet(BULLET, candidate, corpus)
    assert not accepted
    assert reason is not None
    assert "Pillow" in reason


def test_the_refusal_names_what_was_lost(corpus: SourceCorpus) -> None:
    candidate = "Built a companion application with 35 non-destructive editing operations."
    assert "Pillow" in dropped(BULLET, candidate, corpus.technologies)
    assert "Python" in dropped(BULLET, candidate, corpus.technologies)


def test_keeping_the_technology_is_accepted(corpus: SourceCorpus) -> None:
    """Rewording around the library is exactly what tailoring is for."""
    # Reordered, not re-worded: this branch's noun-phrase chunker checks every
    # noun, so introducing one the résumé never used ("image") would be refused
    # as fabrication and this test would stop testing what it is named for.
    candidate = (
        "Built a companion Python application with 35 non-destructive editing "
        "operations (Pillow/Tkinter)."
    )
    accepted, reason, _report = vet(BULLET, candidate, corpus)
    assert accepted, reason


def test_an_equivalent_spelling_is_not_a_deletion(corpus: SourceCorpus) -> None:
    """Expanding an acronym renames a technology; it does not remove one.

    Presence is decided through the alias table, so `CI` becoming `continuous
    integration` keeps the term. Refusing it would punish the one substitution
    the alias table exists to permit.
    """
    original = (
        "Established TypeScript, browser E2E, Rust, linting, and type-checking CI across 200 files."
    )
    candidate = (
        "Established TypeScript, browser E2E, Rust, linting, and type-checking "
        "continuous integration across 200 files."
    )
    assert dropped(original, candidate, corpus.technologies) == ()


def test_a_verb_is_not_a_technology(corpus: SourceCorpus) -> None:
    """The reason this is scoped to listed technologies rather than to names.

    `extract_entities` reads `Filtered` and `Provisioned` as proper nouns
    because they open a sentence. Refusing every dropped proper noun would
    reject ordinary re-emphasis, which §2.1 explicitly permits.
    """
    original = "Filtered 2,338 incident records into a modeling dataset."
    candidate = "Reduced 2,338 incident records into a modeling dataset."
    assert dropped(original, candidate, corpus.technologies) == ()


def test_a_flat_corpus_disables_the_check() -> None:
    """`from_texts` holds no structure, so there is no inventory to read.

    Guessing one from unstructured text would refuse rewrites on a corpus that
    never listed a skill, which is worse than not checking.
    """
    flat = SourceCorpus.from_texts(RESUME)
    assert flat.technologies == frozenset()
    assert dropped(BULLET, "Built a companion application.", flat.technologies) == ()


# --------------------------------------------------------------------------
# The category label is not a technology
# --------------------------------------------------------------------------


LABELLED = """Skills
Frameworks & Web  FastAPI, React, Streamlit, Node.js, Tauri
Data, DevOps & Tools  Qdrant (Vector DB), Docker, Git, pytest, Pillow
Languages (Spoken): English (Professional), Telugu (Native), Hindi (Conversational)
"""


def test_the_heading_above_a_list_is_not_read_as_a_skill() -> None:
    """`Frameworks & Web  FastAPI, React, ...` lists frameworks.

    "Frameworks" and "Web" are the category. Reading them in made `web` a
    technology the résumé was held to keep, and a real rewrite by Gemini was
    refused for "dropping" it — a false refusal produced by the guard, on a
    word the owner never claimed as a skill.

    Both label shapes are covered: the column gap left over from a two-column
    layout, and a plain colon.
    """
    listed = inventory(parse_text(LABELLED))

    for heading in ("web", "frameworks", "data", "devops", "tools", "languages", "spoken"):
        assert heading not in listed, heading


def test_the_entries_after_the_heading_still_are() -> None:
    """The other half — stripping the label must not strip the list."""
    listed = inventory(parse_text(LABELLED))

    for tech in ("fastapi", "react", "streamlit", "tauri", "qdrant", "docker", "pytest", "pillow"):
        assert tech in listed, tech
