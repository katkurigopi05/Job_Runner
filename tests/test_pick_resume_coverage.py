"""The base résumé that backs the most of a posting's direct requirements is the one used.

Read the same way the skill graph reads them: a posting's direct requirements
are `skill_links.posting_links` of its requirements reading (required, not one
choice among several, quoted), and a résumé's skills are `gaps.resume_skills`
of its lines, as `skill_links.link_resume` saves them. Similarity still decides
between résumés that back the same number, and decides alone when a posting
states no direct requirement. A preferred or merely named skill does not count.
"""

from __future__ import annotations

import pytest

from packages.matching.embed import LexicalEmbedder
from packages.matching.pick_resume import choose_base_resume
from tests.test_pick_resume import BACKEND, COMPLETE, ML, STUB, _candidate, _profile, _resume

pytestmark = pytest.mark.asyncio

#: Shares the posting's prose word for word, and lists none of its required tools.
ML_WITH_ANSIBLE = [*ML, "Ansible"]
#: Shares almost no prose with the posting, and lists both required tools.
INFRA = [
    "Platform Engineer.",
    "Kubernetes, Terraform, Docker, AWS.",
    "Ran production clusters.",
]
INFRA_POSTING = (
    "Machine Learning Platform Engineer\n"
    "We train and serve deep learning models for ranking and recommendation on GPU "
    "training clusters.\n"
    "Requirements\n"
    "Kubernetes and Terraform are required.\n"
    "Nice to have\n"
    "Ansible."
)
PYTHON_POSTING = (
    "Backend Engineer\nBuild REST APIs and database schemas.\nRequirements\nPython is required."
)


async def test_the_resume_backing_more_requirements_wins_over_a_closer_one(db_session) -> None:
    candidate = await _candidate(db_session)
    await _resume(db_session, candidate, ML_WITH_ANSIBLE, 1)
    infra = await _resume(db_session, candidate, INFRA, 2)

    choice = await choose_base_resume(
        db_session, _profile(candidate, base=None), INFRA_POSTING, embedder=LexicalEmbedder()
    )

    assert choice is not None and choice.resume_id == infra.id
    assert "2 of 2" in choice.reason
    rendered = choice.as_dict()
    assert rendered["required"] == ["Kubernetes", "Terraform"]
    assert rendered["missing"] == []
    assert [(c["version"], c["covered"]) for c in rendered["considered"]] == [(2, 2), (1, 0)]


async def test_the_closer_resume_was_what_similarity_alone_picked(db_session) -> None:
    """The case above is one coverage decides: with no requirements read, ML wins."""
    candidate = await _candidate(db_session)
    ml = await _resume(db_session, candidate, ML_WITH_ANSIBLE, 1)
    await _resume(db_session, candidate, INFRA, 2)
    prose_only = INFRA_POSTING.split("Requirements")[0]

    choice = await choose_base_resume(
        db_session, _profile(candidate, base=None), prose_only, embedder=LexicalEmbedder()
    )

    assert choice is not None and choice.resume_id == ml.id
    assert choice.as_dict()["required"] == []


async def test_a_preferred_skill_does_not_count(db_session) -> None:
    """ML lists the preferred Ansible; it backs 0 of 2 direct requirements, not 1 of 3."""
    candidate = await _candidate(db_session)
    await _resume(db_session, candidate, ML_WITH_ANSIBLE, 1)
    await _resume(db_session, candidate, INFRA, 2)

    choice = await choose_base_resume(
        db_session, _profile(candidate, base=None), INFRA_POSTING, embedder=LexicalEmbedder()
    )

    assert choice is not None
    covered = {c["version"]: c["covered"] for c in choice.as_dict()["considered"]}
    assert covered == {1: 0, 2: 2}


async def test_equal_coverage_is_decided_by_similarity(db_session) -> None:
    """Both back Python; the backend posting is closer to the backend résumé."""
    candidate = await _candidate(db_session)
    backend = await _resume(db_session, candidate, BACKEND, 1)
    await _resume(db_session, candidate, [*ML, "Python"], 2)

    choice = await choose_base_resume(
        db_session, _profile(candidate, base=None), PYTHON_POSTING, embedder=LexicalEmbedder()
    )

    assert choice is not None and choice.resume_id == backend.id
    assert "1 of 1" in choice.reason and "closest" in choice.reason


async def test_equal_coverage_inside_the_noise_keeps_the_profiles_own_choice(
    db_session,
) -> None:
    candidate = await _candidate(db_session)
    first = await _resume(db_session, candidate, BACKEND, 1)
    await _resume(db_session, candidate, BACKEND, 2)

    choice = await choose_base_resume(
        db_session, _profile(candidate, base=first), PYTHON_POSTING, embedder=LexicalEmbedder()
    )

    assert choice is not None and choice.resume_id == first.id
    assert "profile's own" in choice.reason


async def test_an_unreadable_resume_cannot_win_on_coverage_either(db_session) -> None:
    """The stub backs both requirements and parses at 56%; sendability is settled first."""
    candidate = await _candidate(db_session)
    complete = await _resume(db_session, candidate, COMPLETE, 1)
    await _resume(db_session, candidate, STUB, 2)
    posting = "Platform Engineer\nRequirements\nKubernetes and Docker are required."

    choice = await choose_base_resume(
        db_session, _profile(candidate, base=None), posting, embedder=LexicalEmbedder()
    )

    assert choice is not None and choice.resume_id == complete.id
