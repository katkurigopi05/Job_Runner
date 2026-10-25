"""A repository the owner linked to a résumé project is evidence for that project only.

CLAUDE.md §2.1, as amended on 2026-10-25: the Projects section may add facts a
linked repository's own GitHub record reports, its dependency files included,
attributed to that project. Never to another project, never to an employer.
"""

from __future__ import annotations

import uuid

import pytest

from packages.tailor.cache import tailoring_key
from packages.tailor.evidence import evidence_digest
from packages.tailor.guard import SourceCorpus, entry_key
from packages.tailor.parse import parse_text
from packages.tailor.rewrite import vet
from packages.tailor.targeted import Status, classify, judge

RESUME = """Jordan Avery
jordan@example.com | (555) 123-4567

PROJECTS
Attorney.AI — Legal Research Assistant   [GitHub]
Python, FastAPI
• Built a legal research assistant with hybrid retrieval over court opinions.
StreamBreaker AI — Music Marketing Tool   [GitHub]
Python, Streamlit
• Built the Streamlit interface for a music marketing tool.

EXPERIENCE
Data Analyst, Acme Corp Jan 2023 – Dec 2024
• Built reporting dashboards for the finance team.

SKILLS
Languages  Python, SQL
Tools  Git, Docker, Tableau
"""

ATTORNEY = "• Built a legal research assistant with hybrid retrieval over court opinions."
STREAMBREAKER = "• Built the Streamlit interface for a music marketing tool."
ACME = "• Built reporting dashboards for the finance team."

#: What `evidence.linked_evidence` returns for one linked repository.
LINKED = {"Attorney.AI": "Attorney.AI\nCitation-first legal RAG\nPython\nLlamaIndex\nQdrant"}


@pytest.fixture(scope="module")
def parsed():
    return parse_text(RESUME)


def _vet(corpus: SourceCorpus, original: str, candidate: str) -> tuple[bool, str | None]:
    accepted, reason, _ = vet(original.lstrip("• "), candidate, corpus)
    return accepted, reason


# --------------------------------------------------------------------------
# The guard
# --------------------------------------------------------------------------


def test_a_linked_project_may_name_what_its_repository_declares(parsed) -> None:
    corpus = SourceCorpus.from_resume(parsed, linked=LINKED)
    accepted, reason = _vet(
        corpus,
        ATTORNEY,
        (
            "Built a legal research assistant with hybrid retrieval over court opinions,"
            " using LlamaIndex."
        ),
    )
    assert accepted, reason


def test_the_repository_backs_the_tool_not_a_new_claim_about_it(parsed) -> None:
    # The repository says the project uses LlamaIndex. It does not say the
    # hybrid retrieval is LlamaIndex's, and the recombination check holds that.
    corpus = SourceCorpus.from_resume(parsed, linked=LINKED)
    accepted, reason = _vet(
        corpus,
        ATTORNEY,
        "Built a legal research assistant with LlamaIndex hybrid retrieval over court opinions.",
    )
    assert not accepted
    assert reason and "never together" in reason


def test_without_the_link_the_same_claim_is_refused(parsed) -> None:
    corpus = SourceCorpus.from_resume(parsed)
    accepted, _ = _vet(
        corpus,
        ATTORNEY,
        (
            "Built a legal research assistant with hybrid retrieval over court opinions,"
            " using LlamaIndex."
        ),
    )
    assert not accepted


def test_another_project_may_not_borrow_the_linked_repository(parsed) -> None:
    corpus = SourceCorpus.from_resume(parsed, linked=LINKED)
    accepted, _ = _vet(
        corpus,
        STREAMBREAKER,
        "Built the Streamlit interface for a LlamaIndex music marketing tool.",
    )
    assert not accepted


def test_an_employer_may_not_borrow_it_either(parsed) -> None:
    corpus = SourceCorpus.from_resume(parsed, linked=LINKED)
    accepted, _ = _vet(corpus, ACME, "Built LlamaIndex reporting dashboards for the finance team.")
    assert not accepted


def test_a_link_naming_an_employer_does_nothing(parsed) -> None:
    corpus = SourceCorpus.from_resume(parsed, linked={"Data Analyst, Acme Corp": "LlamaIndex"})
    accepted, _ = _vet(corpus, ACME, "Built LlamaIndex reporting dashboards for the finance team.")
    assert not accepted


def test_linking_never_withdraws_a_skills_word_from_other_entries(parsed) -> None:
    # Docker is in Skills and claimed by no entry, so §2.1 lets any entry use
    # it. The repository declaring it must not change that for StreamBreaker.
    candidate = "Built the Streamlit interface for a music marketing tool with Docker."
    before = _vet(SourceCorpus.from_resume(parsed), STREAMBREAKER, candidate)
    after = _vet(
        SourceCorpus.from_resume(parsed, linked={"Attorney.AI": "Docker"}),
        STREAMBREAKER,
        candidate,
    )
    assert before[0] and after[0]


def test_no_links_builds_the_corpus_it_always_did(parsed) -> None:
    plain = SourceCorpus.from_resume(parsed)
    empty = SourceCorpus.from_resume(parsed, linked={})
    assert plain.tokens == empty.tokens
    assert plain.shared == empty.shared
    assert plain.attributed == empty.attributed
    assert [i.tokens for i in plain.items] == [i.tokens for i in empty.items]


@pytest.mark.parametrize(
    "name",
    [
        "Attorney.AI",
        "attorney.ai",
        "  Attorney.AI  ",
        "Attorney.AI — Legal Research Assistant   [GitHub]",
        "Attorney.AI — Legal Research Assistant",
    ],
)
def test_an_entry_is_found_by_its_name_however_the_link_wrote_it(name: str) -> None:
    assert entry_key(name) == entry_key("Attorney.AI — Legal Research Assistant   [GitHub]")


def test_two_different_projects_never_share_a_key() -> None:
    assert entry_key("Attorney.AI — Legal") != entry_key("StreamBreaker AI — Music")


# --------------------------------------------------------------------------
# Targeted tailoring
# --------------------------------------------------------------------------

POSTING = """Software Engineer, AI.
Requirements: experience with LlamaIndex and Qdrant for retrieval systems."""

#: The owner's shape: no Experience section, so Projects is what is tailored
#: (`bullets.tailorable_section`). With an employer present, project bullets
#: are never sent at all.
PROJECTS_ONLY = RESUME.split("EXPERIENCE", 1)[0] + "SKILLS" + RESUME.split("SKILLS", 1)[1]


@pytest.fixture(scope="module")
def projects_only():
    return parse_text(PROJECTS_ONLY)


def test_a_term_a_linked_repository_backs_is_targeted_at_that_project(projects_only) -> None:
    plan = classify(projects_only, POSTING, linked=LINKED)
    term = next(t for t in plan.terms if t.term.lower() == "llamaindex")
    assert term.status is Status.SUPPORTED
    assert term.bullet is not None
    assert "legal research assistant" in plan.bullets[term.bullet]


def test_without_the_link_it_is_not_targeted(projects_only) -> None:
    plan = classify(projects_only, POSTING)
    term = next(t for t in plan.terms if t.term.lower() == "llamaindex")
    assert term.bullet is None


def test_the_revision_may_add_the_term_to_that_project(projects_only) -> None:
    plan = classify(projects_only, POSTING, linked=LINKED)
    index = next(t.bullet for t in plan.terms if t.term.lower() == "llamaindex")
    bullet = plan.bullets[index]
    targets = plan.targets[index]
    off_limits = tuple(
        t.term for t in plan.terms if t.term not in {target.term for target in targets}
    )
    added, reason, _, _ = judge(
        bullet,
        bullet.replace("court opinions.", "court opinions, using LlamaIndex."),
        targets,
        SourceCorpus.from_resume(projects_only, linked=LINKED),
        off_limits,
    )
    assert "LlamaIndex" in added, reason


# --------------------------------------------------------------------------
# The cache
# --------------------------------------------------------------------------


def test_links_change_the_cache_key_and_no_links_leave_it_as_it_was() -> None:
    resume_id = uuid.uuid4()
    args = dict(source_resume_id=resume_id, content_hash="h", projects=[], provider="p", model="m")
    plain = tailoring_key(**args)
    assert tailoring_key(**args, evidence=evidence_digest({})) == plain
    assert tailoring_key(**args, evidence=evidence_digest(LINKED)) != plain


def test_the_digest_does_not_depend_on_order() -> None:
    two = {"A": "x", "B": "y"}
    assert evidence_digest(two) == evidence_digest(dict(reversed(list(two.items()))))


def test_the_streambreaker_bullet_is_not_targeted_for_attorneys_repository(projects_only) -> None:
    plan = classify(projects_only, POSTING, linked=LINKED)
    streambreaker = next(i for i, b in enumerate(plan.bullets) if "Streamlit interface" in b)
    assert streambreaker not in plan.targets


# --------------------------------------------------------------------------
# What the dashboard offers to link
# --------------------------------------------------------------------------


def test_the_entries_offered_are_the_projects_the_guard_reads(parsed) -> None:
    from packages.tailor.guard import project_names

    assert project_names(parsed) == ["Attorney.AI", "StreamBreaker AI"]


def test_a_suggestion_needs_the_same_name_not_a_similar_one() -> None:
    from packages.core.models import Project
    from packages.tailor.evidence import suggest_links

    def repo(name: str, *, is_fork: bool = False) -> Project:
        return Project(id=uuid.uuid4(), name=name, is_fork=is_fork, resume_entry=None)

    exact, spaced, near, fork, linked = (
        repo("Attorney.AI"),
        repo("StreambreakerAi"),
        repo("attorney-ai-v2"),
        repo("streambreaker-ai", is_fork=True),
        repo("streambreaker_ai"),
    )
    linked.resume_entry = "Something else"
    suggested = suggest_links(
        ["Attorney.AI", "StreamBreaker AI"], [exact, spaced, near, fork, linked]
    )
    assert suggested == {exact.id: "Attorney.AI", spaced.id: "StreamBreaker AI"}
