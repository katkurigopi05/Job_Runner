"""The keyword score reads the skills a posting asks for by name.

Measured on 2026-11-12 over the owner's 300 best-scored open matches: of the
terms the score held the résumé to, a median of 3% were skills, and a median
of 17% of the skills a posting required or preferred were among them. Python
was required by 85 postings whose list never showed it, because the list is
the forty most repeated words and a posting names its stack once.

`matching/requirements.py` already reads a posting's skills, and whether each
is required, preferred or only mentioned. The score now starts its list with
the first two kinds.
"""

from __future__ import annotations

from packages.tailor import ats
from packages.tailor.ats import posting_skills, requirement_terms, score
from packages.tailor.parse import parse_text
from packages.tailor.targeted import posting_terms

RESUME = """\
Jane Doe
jane@example.com | (555) 123-4567 | github.com/janedoe

EXPERIENCE
Acme Corp, San Francisco, CA
Software Engineer, Jan 2021 - Present
Built payment services in Python and Postgres.

EDUCATION
State University, Bachelor of Science in Computer Science, Aug 2016 - May 2020

SKILLS
Python, PostgreSQL, Docker
"""

#: Forty-five words a posting can repeat without asking for anything.
_PROSE_TEXT = """
latency throughput roadmap stakeholders milestones ownership delivery mentorship
architecture reliability scalability observability documentation collaboration
prioritization estimation retrospectives incidents postmortems dashboards budgets
vendors compliance governance migrations refactoring onboarding planning strategy
execution alignment visibility accountability curiosity craftsmanship initiative
judgement empathy clarity momentum outcomes tradeoffs feedback autonomy rigor
"""
_PROSE = _PROSE_TEXT.split()

#: Every prose word twice and each skill once: the frequency cut keeps the prose.
CROWDED_POSTING = (
    "Requirements\n"
    + "".join(f"- Cares about {word} and owns {word}.\n" for word in _PROSE)
    + "- Python and Kubernetes are required.\n"
    + "Nice to have\n"
    + "- Familiarity with Terraform.\n"
)


def _lowered(terms: list[str]) -> list[str]:
    return [term.lower() for term in terms]


def test_a_skill_named_once_is_held_against_the_resume() -> None:
    """The defect, stated as the thing that must not come back."""
    assert len(_PROSE) > 40, "the prose alone has to fill the list"

    terms = _lowered(requirement_terms(CROWDED_POSTING))

    for skill in ("python", "kubernetes", "terraform"):
        assert skill in terms, f"{skill} is asked for by name and was not scored"


def test_required_skills_come_before_preferred_ones_and_before_prose() -> None:
    terms = _lowered(requirement_terms(CROWDED_POSTING))

    assert terms[:3] == ["python", "kubernetes", "terraform"]


def test_the_list_is_no_longer_than_it_was() -> None:
    """Skills take places from the least repeated words, they do not add to them."""
    assert len(requirement_terms(CROWDED_POSTING)) <= 40
    assert len(requirement_terms(CROWDED_POSTING, limit=5)) == 5


def test_a_skill_is_written_as_the_posting_writes_it() -> None:
    """The posting's spelling is the one an ATS scans for and a revision may use."""
    posting = "Requirements\n- Strong Postgres and K8s experience is required.\n"

    assert [ask.term for ask in posting_skills(posting)] == ["Postgres", "K8s"]


def test_a_capital_that_only_opens_a_line_is_not_part_of_the_spelling() -> None:
    """A targeted revision writes the term into the middle of a bullet."""
    posting = (
        "Requirements\n"
        "- Deep learning experience is required.\n"
        "- Hugging Face and Power BI are required.\n"
    )

    assert [ask.term for ask in posting_skills(posting)] == [
        "Power BI",
        "Hugging Face",
        "deep learning",
    ]


def test_the_words_of_an_asked_skill_are_not_requirements_of_their_own() -> None:
    """ "deep" was scored in 38 of the owner's 300 best matches, beside "learning"."""
    posting = (
        "Requirements\n"
        "- Deep learning is required: deep learning models, deep learning systems.\n"
        "- Careful estimates.\n"
    )

    terms = _lowered(requirement_terms(posting))

    assert "deep learning" in terms
    assert "deep" not in terms and "learning" not in terms
    assert "estimates" in terms, "the words beside it are still read"


def test_an_asked_skill_that_is_a_word_of_another_is_still_asked() -> None:
    """Ruby is not dropped for being a word of Ruby on Rails."""
    posting = "Requirements\n- Ruby is required.\n- Ruby on Rails is required.\n"

    assert [ask.key for ask in posting_skills(posting)] == ["ruby", "rails"]
    assert requirement_terms(posting) == ["Ruby", "Rails"]


def test_a_second_spelling_of_an_asked_skill_is_still_scanned_for() -> None:
    """A posting that writes both is read for both, as it was before."""
    posting = "Requirements\n- Kubernetes is required; K8s operators, K8s upgrades.\n"

    terms = _lowered(requirement_terms(posting))

    assert "kubernetes" in terms and "k8s" in terms


def test_a_skill_the_posting_only_mentions_is_not_a_demand() -> None:
    """ "We build with Go" in a company blurb asks the applicant for nothing."""
    posting = (
        "About us\nWe build our platform with Go and ship it weekly.\n"
        "Requirements\n- Python is required.\n"
    )

    assert [ask.key for ask in posting_skills(posting)] == ["python"]


def test_required_and_preferred_are_told_apart() -> None:
    asks = {ask.key: ask.required for ask in posting_skills(CROWDED_POSTING)}

    assert asks == {"python": True, "kubernetes": True, "terraform": False}


def test_a_skill_the_posting_repeats_is_listed_once() -> None:
    """The frequency list would name it again under the same or another spelling."""
    posting = (
        "Requirements\n"
        "- Python is required. Python services, Python tooling, Python reviews.\n"
        "- Machine learning experience is required; ML systems, ML platforms, ML teams.\n"
    )

    terms = _lowered(requirement_terms(posting))

    assert terms.count("python") == 1
    assert sum(term in ("machine learning", "ml") for term in terms) == 1


def test_a_posting_that_states_no_skill_is_scored_as_before() -> None:
    posting = "Requirements\n- Clear writing.\n- Careful estimates and steady delivery.\n"

    report = score(parse_text(RESUME), posting)

    assert posting_skills(posting) == []
    assert report.states_skills is False
    assert report.skills == 0.0, "nothing asked is not nothing matched, and the flag says which"
    assert report.skills_supported == [] and report.skills_missing == []


def test_the_score_says_which_asked_skills_the_resume_backs() -> None:
    report = score(parse_text(RESUME), CROWDED_POSTING)

    assert report.states_skills is True
    assert _lowered(report.skills_supported) == ["python"]
    assert _lowered(report.skills_missing) == ["kubernetes", "terraform"]
    assert report.skills == round(1 / 3, 3)


def test_skills_are_counted_among_the_terms_and_not_beside_them() -> None:
    """One verdict per term: a skill cannot be backed in one list and missing in the other."""
    report = score(parse_text(RESUME), CROWDED_POSTING)

    assert set(report.skills_supported) <= set(report.supported)
    assert set(report.skills_missing) <= set(report.missing)


def test_an_alias_backs_an_asked_skill() -> None:
    """The résumé says PostgreSQL and Postgres; the posting's spelling is still backed."""
    posting = "Requirements\n- Postgres is required.\n"

    assert score(parse_text(RESUME), posting).skills_supported == ["Postgres"]


def test_the_summary_names_the_skills_when_a_posting_states_any() -> None:
    assert "skills 1 of 3" in score(parse_text(RESUME), CROWDED_POSTING).summary()
    assert "skills" not in score(parse_text(RESUME), "").summary()


def test_the_targeted_revision_reads_the_same_list() -> None:
    """`targeted.classify` sorts these terms; it and the score cannot read two postings."""
    terms = _lowered(posting_terms(CROWDED_POSTING))

    assert terms[:3] == ["python", "kubernetes", "terraform"]


def test_the_recruiter_scan_reads_the_same_list() -> None:
    """A second copy of the list in `recruiter.py` would not have gained the skills."""
    from packages.tailor import recruiter

    seen: list[str] = []
    original = recruiter._ten_second_scan

    def capture(lines, wanted, findings):
        seen.extend(sorted(wanted))
        return original(lines, wanted, findings)

    recruiter._ten_second_scan = capture
    try:
        recruiter.score(parse_text(RESUME), CROWDED_POSTING)
    finally:
        recruiter._ten_second_scan = original

    assert {"python", "kubernetes", "terraform"} <= set(seen)


def test_the_list_carries_a_revision_for_the_cache() -> None:
    """A targeted document depends on this list, so its cache key has to name it."""
    assert isinstance(ats.TERMS_REVISION, str) and ats.TERMS_REVISION
