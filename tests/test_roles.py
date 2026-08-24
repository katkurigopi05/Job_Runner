"""Same role, different name.

`packages/matching/roles.py` exists because two of this project's stages read
job titles as strings. The keyword filter in `search.py` is a substring test,
and the title half of the score is a cosine over `embed.py`, whose shipped
backend hashes tokens into buckets. Under that backend "SDE II" scores 0.000
against "Software Engineer" — indistinguishable from "Dental Hygienist", since
neither shares a token with it.

The filter is the more serious of the two: it runs *before* scoring, so a
posting it drops is never scored at all and no change of embedding backend can
bring it back.

These tests hold both directions. Synonyms must survive, and roles that merely
sound alike must stay apart — `Data Engineer` and `Data Scientist` are the pair
any embedding would merge and the owner would most regret merging.
"""

from __future__ import annotations

import uuid

from packages.core.models import Posting, Profile
from packages.matching.embed import LexicalEmbedder
from packages.matching.roles import canonical, mine_aliases, normalize, roles_in, same_role
from packages.matching.score import ROLE_MATCH_FLOOR, score_posting
from packages.matching.search import SearchFilters, matches


def posting(title: str = "Software Engineer", description: str = "Python.", **kwargs) -> Posting:
    return Posting(
        id=uuid.uuid4(),
        url=f"https://boards.greenhouse.io/acme/jobs/{uuid.uuid4().hex[:6]}",
        title=title,
        description_raw=description,
        location=kwargs.pop("location", "Remote"),
        **kwargs,
    )


def profile(**kwargs) -> Profile:
    defaults = dict(
        id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        label="engineering",
        location="Austin, TX",
        work_auth="US citizen",
        needs_sponsorship=False,
        links_json={},
        answers_kv_json={},
    )
    defaults.update(kwargs)
    return Profile(**defaults)


RESUME = """
Software Engineer, Analytical Engines Ltd
Built async APIs with FastAPI, deployed on Kubernetes.
Skills: Python, PostgreSQL, FastAPI, Docker
"""


# --- canonicalization -------------------------------------------------------


def test_synonyms_share_one_canonical_role() -> None:
    """The titles the lexical embedder scores 0.000 against each other."""
    for title in (
        "Software Engineer",
        "Software Development Engineer",
        "SDE II",
        "Member of Technical Staff",
        "Programmer Analyst",
        "Applications Developer",
    ):
        assert canonical(title) == "software_engineer", title


def test_adjacent_roles_are_not_merged() -> None:
    """The distinction any embedding blurs and the owner cannot afford to lose.

    A missed synonym costs one posting. A merged role silently reroutes a whole
    feed, and nothing downstream reports that it happened.
    """
    assert canonical("Data Engineer") == "data_engineer"
    assert canonical("Data Scientist") == "data_scientist"
    assert canonical("Data Analyst") == "data_analyst"
    assert canonical("Machine Learning Engineer") == "machine_learning_engineer"
    assert not same_role("Data Engineer", "Data Scientist")
    assert not same_role("Data Analyst", "Data Engineer")


def test_seniority_and_level_markers_are_not_part_of_the_role() -> None:
    """Seniority is already a filter of its own; it must not split a role."""
    for title in ("Senior Data Engineer", "Sr. Data Engineer", "Data Engineer III"):
        assert canonical(title) == "data_engineer", title
    assert normalize("Senior Software Engineer II (Remote) - Platform") == "software engineer"


def test_a_rung_word_inside_a_role_name_survives() -> None:
    """ "Staff" is a rung, but "Member of Technical Staff" is a role name.

    Stripping levels before looking the alias up turned this title into
    "member of technical" and lost it entirely.
    """
    assert canonical("Member of Technical Staff") == "software_engineer"
    assert canonical("Staff Software Engineer") == "software_engineer"


def test_an_unreadable_title_never_matches_anything() -> None:
    """None is an answer, not a wildcard."""
    assert canonical("Dental Hygienist") is None
    assert canonical("") is None
    assert not same_role("Dental Hygienist", "Software Engineer")
    assert not same_role("Chief Vibes Officer", "Underwater Basket Weaver")


def test_roles_in_reads_every_role_a_resume_evidences() -> None:
    assert roles_in(RESUME) == {"software_engineer"}
    assert roles_in("no job titles here at all") == set()


# --- the pre-scoring filter -------------------------------------------------


def test_role_synonym_survives_the_keyword_filter() -> None:
    """The defect no embedding backend could fix, because it precedes scoring."""
    filters = SearchFilters(keywords=("software engineer",))

    verdict = matches(posting(title="Member of Technical Staff"), filters)

    assert verdict.kept, verdict.reasons


def test_the_keyword_filter_still_drops_an_unrelated_posting() -> None:
    """Widening the filter must not turn it off."""
    filters = SearchFilters(keywords=("software engineer",))

    verdict = matches(posting(title="Dental Hygienist", description="Cleanings."), filters)

    assert not verdict.kept
    assert any("software engineer" in reason for reason in verdict.reasons)


def test_the_keyword_filter_does_not_merge_adjacent_roles() -> None:
    filters = SearchFilters(keywords=("data engineer",))

    verdict = matches(posting(title="Data Scientist", description="Statistics."), filters)

    assert not verdict.kept


# --- scoring ----------------------------------------------------------------


def test_role_agreement_floors_the_title_similarity() -> None:
    """A curated alias outranks a cosine over three words."""
    embedder = LexicalEmbedder()
    me = embedder.encode([RESUME])[0]

    result = score_posting(
        posting(title="Member of Technical Staff", description="Python, FastAPI, Docker."),
        profile(),
        me,
        embedder,
        profile_text_value=RESUME,
    )

    assert result.role_match == "software_engineer"
    assert result.title_similarity >= ROLE_MATCH_FLOOR


def test_a_role_the_resume_does_not_evidence_is_not_floored() -> None:
    """The floor needs agreement, not merely a readable title."""
    embedder = LexicalEmbedder()
    me = embedder.encode([RESUME])[0]

    result = score_posting(
        posting(title="Site Reliability Engineer", description="Terraform, on-call."),
        profile(),
        me,
        embedder,
        profile_text_value=RESUME,
    )

    assert result.role_match is None
    assert result.title_similarity < ROLE_MATCH_FLOOR


def test_the_floor_is_recorded_so_a_ranking_can_be_explained() -> None:
    """A floored similarity must not read as a genuinely high cosine."""
    embedder = LexicalEmbedder()
    me = embedder.encode([RESUME])[0]

    result = score_posting(
        posting(title="SDE II", description="Python."),
        profile(),
        me,
        embedder,
        profile_text_value=RESUME,
    )

    assert result.reasons()["role_match"] == "software_engineer"


# --- mining -----------------------------------------------------------------


def test_mine_aliases_proposes_an_unknown_title_that_describes_a_known_role() -> None:
    """Two companies describing one job in the same words are naming one role."""
    body = "Design ETL pipelines, model the warehouse, own dbt and Airflow."
    postings = [
        posting(title="Data Engineer", description=body),
        posting(title="Data Engineer", description=body),
        posting(title="Pipeline Wrangler", description=body),
        posting(title="Pipeline Wrangler", description=body),
    ]

    proposals = mine_aliases(postings)

    assert proposals
    assert proposals[0].proposed_alias == "pipeline wrangler"
    assert proposals[0].support >= 2


def test_mine_aliases_ignores_a_single_unsupported_pair() -> None:
    """One coincidence is not evidence."""
    body = "Design ETL pipelines, model the warehouse, own dbt and Airflow."
    postings = [
        posting(title="Data Engineer", description=body),
        posting(title="Pipeline Wrangler", description=body),
    ]

    assert mine_aliases(postings, min_support=2) == []


def test_mine_aliases_never_edits_the_table() -> None:
    """It proposes to a human. Adoption is a hand edit, on purpose."""
    from packages.matching import roles

    before = dict(roles.ROLE_ALIASES)
    body = "Design ETL pipelines, model the warehouse, own dbt and Airflow."
    mine_aliases([posting(title="Data Engineer", description=body)] * 2)

    assert before == roles.ROLE_ALIASES
