"""Matching — Gate 5's scoring half.

Gate 5 asks that scores are "sane against a hand-labeled set of 20 postings —
the ones you'd actually apply to rank in the top 10". `test_gate5_hand_labeled`
is that check, run against the default lexical embedder.

Worth being precise about what that proves. The default embedder measures
**vocabulary overlap, not meaning**, so it ranks well when a posting shares
the profile's actual words and poorly when it says the same thing differently.
The hand-labeled fixtures are written the way real postings are, but a
semantic embedder would score differently — better on paraphrase, and it needs
re-embedding to switch.
"""

from __future__ import annotations

import uuid

import pytest

from packages.core.models import Posting, Profile
from packages.matching.embed import LexicalEmbedder, cosine, tokenize
from packages.matching.filters import (
    apply_filters,
    clearance_ok,
    detect_seniority,
    is_remote,
    location_matches,
    seniority_ok,
    sponsorship_ok,
)
from packages.matching.labels import load_labeled_set
from packages.matching.score import (
    TITLE_WEIGHT,
    ScoredPosting,
    keyword_overlap,
    missing_terms,
    score_posting,
)


def posting(
    title: str = "Senior Backend Engineer",
    description: str = "Python, PostgreSQL, distributed systems.",
    location: str = "Remote",
    **kwargs,
) -> Posting:
    return Posting(
        id=uuid.uuid4(),
        url=f"https://boards.greenhouse.io/acme/jobs/{uuid.uuid4().hex[:6]}",
        title=title,
        description_raw=description,
        location=location,
        **kwargs,
    )


def profile(**kwargs) -> Profile:
    defaults = dict(
        id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        label="backend",
        location="Austin, TX",
        work_auth="US citizen",
        needs_sponsorship=False,
        links_json={},
        answers_kv_json={},
    )
    defaults.update(kwargs)
    return Profile(**defaults)


PROFILE_TEXT = """
Staff Engineer, Analytical Engines Ltd
Designed a note-taking subsystem handling millions of events per day in Python.
Migrated the billing service from MySQL to PostgreSQL with zero downtime.
Built async APIs with FastAPI, deployed on Kubernetes and Docker.
Skills: Python, PostgreSQL, FastAPI, Docker, Kubernetes, async, backend, distributed systems
"""


# --------------------------------------------------------------------------
# Embedding
# --------------------------------------------------------------------------


def test_tokenize_drops_stopwords() -> None:
    tokens = tokenize("We are looking for a Python engineer with experience")
    assert "python" in tokens
    assert "are" not in tokens
    assert "experience" not in tokens


def test_encoding_is_deterministic_across_instances() -> None:
    """Vectors are persisted, so they must match on the next process too."""
    a = LexicalEmbedder().encode(["Python backend engineer"])[0]
    b = LexicalEmbedder().encode(["Python backend engineer"])[0]
    assert a == b


def test_vectors_are_unit_length() -> None:
    vector = LexicalEmbedder().encode(["Python PostgreSQL Docker"])[0]
    assert sum(v * v for v in vector) == pytest.approx(1.0, abs=1e-6)


def test_empty_text_encodes_to_zero() -> None:
    assert LexicalEmbedder().encode([""])[0] == [0.0] * 384


def test_similar_text_scores_higher_than_unrelated() -> None:
    embedder = LexicalEmbedder()
    me = embedder.encode([PROFILE_TEXT])[0]
    close = embedder.encode(["Backend engineer, Python, PostgreSQL, Kubernetes"])[0]
    far = embedder.encode(["Pastry chef for a busy bakery, early mornings"])[0]

    assert cosine(me, close) > cosine(me, far)


def test_cosine_edge_cases() -> None:
    assert cosine([], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


# --------------------------------------------------------------------------
# Hard filters
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Software Engineering Intern", "intern"),
        ("Junior Developer", "junior"),
        ("Senior Backend Engineer", "senior"),
        ("Staff Engineer", "senior"),
        ("Principal Architect", "principal"),
        ("Backend Developer", None),
    ],
)
def test_detect_seniority(text: str, expected: str | None) -> None:
    assert detect_seniority(text) == expected


def test_remote_is_detected() -> None:
    assert is_remote(posting(location="Remote - US"))
    assert not is_remote(posting(location="New York, NY"))


# --------------------------------------------------------------------------
# The search area: California in any working mode, the rest of the United
# States remote only, nothing abroad.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    ["San Francisco, CA", "Los Angeles, CA", "San Jose, CA", "San Diego, California"],
)
def test_california_is_reachable_on_site(location: str) -> None:
    """The one place an on-site role costs nothing to accept."""
    assert location_matches(profile(), posting(location=location))


@pytest.mark.parametrize("location", ["Remote — CA", "Remote (California)"])
def test_california_is_reachable_remotely_too(location: str) -> None:
    assert location_matches(profile(), posting(location=location))


@pytest.mark.parametrize("location", ["Austin, TX", "Chicago, IL", "New York, NY", "Seattle, WA"])
def test_another_state_on_site_is_excluded(location: str) -> None:
    """A job in another state is a move, not a commute."""
    assert not location_matches(profile(), posting(location=location))


@pytest.mark.parametrize(
    "location", ["Remote - US", "Remote, USA", "Remote - United States", "US Remote"]
)
def test_another_state_remotely_is_reachable(location: str) -> None:
    assert location_matches(profile(), posting(location=location))


@pytest.mark.parametrize(
    "location",
    [
        "Canada - Remote (ON, AB, BC, or NS Only)",
        "Remote (India only)",
        "Remote - EMEA",
        "Remote - APAC",
    ],
)
def test_remoteness_does_not_override_the_region(location: str) -> None:
    """The defect this filter was rewritten for.

    Every one of these is a *remote* posting, and the previous version
    short-circuited to "matches" the moment it saw the word — so a US-based
    owner was shown Canadian, Indian and EMEA-only roles they are not
    eligible to hold. `Canada - Remote (ON, AB, BC, or NS Only)` is the real
    one: it came out of the crawled Palantir board and was the single posting
    that survived the filter for an Austin profile.
    """
    assert not location_matches(profile(), posting(location=location))


@pytest.mark.parametrize("location", ["Berlin, Germany", "London", "Sydney, Australia"])
def test_abroad_is_excluded(location: str) -> None:
    assert not location_matches(profile(), posting(location=location))


def test_an_unrecognized_place_name_is_kept_rather_than_hidden() -> None:
    """Reversed after the crawl that extended the foreign vocabulary.

    `UNPLACED` used to be dropped on the evidence that every unplaceable
    string in an early sweep was foreign. Thirty-odd countries plus continents
    and blocs have since moved into `ELSEWHERE`, so `UNPLACED` now means
    closer to "a town nobody listed" — and dropping it made every gap in the
    hand-written city lists a silently discarded job.
    """
    assert location_matches(profile(), posting(location="Wakanda City"))


@pytest.mark.parametrize("location", ["Remote", "", "Hybrid / Flexible"])
def test_a_posting_that_names_no_place_is_kept(location: str) -> None:
    """Silence is not evidence against; do not exclude on a guess.

    A bare "Remote" names a working mode, not a place. It used to classify as
    `UNPLACED` — the class reserved for unrecognized place *names*, which the
    corpus says are foreign — and so was dropped, losing the commonest way a
    domestic board writes exactly the job the owner is looking for.
    """
    assert location_matches(profile(), posting(location=location))


def test_the_search_area_does_not_read_the_profile() -> None:
    """§1: filters are the owner's input, not a reading of their profile.

    The old filter compared the posting against `profile.location`, so moving
    house silently rewrote the feed. The area is now stated once, and where
    the owner happens to live does not enter into it.
    """
    assert location_matches(profile(), posting(location="San Francisco, CA"))
    assert not location_matches(profile(), posting(location="Austin, TX"))


def test_sponsorship_exclusion_only_on_an_explicit_statement() -> None:
    needs = profile(needs_sponsorship=True)
    assert not sponsorship_ok(
        needs, posting(description="We are unable to sponsor visas for this role.")
    )
    assert sponsorship_ok(needs, posting(description="Great team, great benefits."))


def test_sponsorship_is_irrelevant_when_not_needed() -> None:
    assert sponsorship_ok(
        profile(needs_sponsorship=False),
        posting(description="No visa sponsorship available."),
    )


def test_clearance_roles_are_excluded() -> None:
    assert not clearance_ok(posting(description="Requires an active TS/SCI clearance."))
    assert clearance_ok(posting(description="No clearance needed."))


def test_seniority_tolerance() -> None:
    assert seniority_ok(posting(title="Senior Backend Engineer"), "senior")
    assert seniority_ok(posting(title="Principal Engineer"), "senior")  # one rung
    assert not seniority_ok(posting(title="Software Engineering Intern"), "senior")


def test_filters_collect_every_reason() -> None:
    verdict = apply_filters(
        profile(needs_sponsorship=True),
        posting(
            location="Berlin, Germany",
            description="Requires TS/SCI clearance. We cannot provide sponsorship.",
        ),
    )

    assert not verdict
    assert len(verdict.reasons) == 3


def test_closed_postings_are_excluded() -> None:
    from datetime import UTC, datetime

    verdict = apply_filters(profile(), posting(closed_at=datetime.now(UTC)))
    assert not verdict
    assert "closed" in verdict.reasons[0]


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def test_excluded_postings_score_exactly_zero() -> None:
    """Never a middling score that could sneak past a threshold."""
    embedder = LexicalEmbedder()
    me = embedder.encode([PROFILE_TEXT])[0]

    result = score_posting(
        posting(description="Requires TS/SCI clearance."), profile(), me, embedder
    )

    assert result.score == 0.0
    assert result.excluded
    assert result.excluded_by


def test_relevant_posting_outscores_irrelevant() -> None:
    embedder = LexicalEmbedder()
    me = embedder.encode([PROFILE_TEXT])[0]
    subject = profile()

    good = score_posting(
        posting(
            title="Senior Backend Engineer",
            description="Python, PostgreSQL, FastAPI, Kubernetes, distributed systems.",
        ),
        subject,
        me,
        embedder,
    )
    bad = score_posting(
        posting(title="Pastry Chef", description="Croissants, early mornings, bakery."),
        subject,
        me,
        embedder,
    )

    assert good.score > bad.score


def test_reasons_are_recorded() -> None:
    embedder = LexicalEmbedder()
    me = embedder.encode([PROFILE_TEXT])[0]
    result = score_posting(posting(), profile(), me, embedder)

    reasons = result.reasons()
    assert "title_similarity" in reasons
    assert "body_similarity" in reasons


def test_title_and_body_weights_sum_to_one() -> None:
    from packages.matching.score import BODY_WEIGHT

    assert pytest.approx(1.0) == TITLE_WEIGHT + BODY_WEIGHT


def test_keyword_overlap_explains_a_match() -> None:
    shared = keyword_overlap(
        PROFILE_TEXT, posting(description="Python and PostgreSQL on Kubernetes")
    )
    assert "python" in shared
    assert "postgresql" in shared


# --------------------------------------------------------------------------
# Gate 5 — hand-labeled ranking
# --------------------------------------------------------------------------

#: The twenty Gate 5 postings, read from the one place they are defined.
#:
#: They used to be a literal list here. They moved to `seeds/labeled_matches.yaml`
#: when the benchmark in `packages/matching/benchmark.py` started needing the
#: same labels: two copies of a labeled set drift, and a gate asserting against
#: a stale copy of the corpus the benchmark reports on is worse than either.
#:
#: The gate selects by tag rather than taking the whole file. The file also
#: holds adjacent roles added for the benchmark, and pulling those in here
#: would silently change what "10 of 20" means.
#:
#: `True` means "I would actually apply to this" — the file grades 0-3, and
#: anything above 0 is wanted.
_LABELED = load_labeled_set("seeds/labeled_matches.yaml")

HAND_LABELED: list[tuple[bool, str, str]] = [
    (item.relevance > 0, item.title, item.description)
    for item in sorted(_LABELED.tagged("gate5"), key=lambda i: i.key)
]


def test_gate5_hand_labeled() -> None:
    """Gate 5: the postings I'd apply to rank in the top 10 of 20."""
    embedder = LexicalEmbedder()
    me = embedder.encode([PROFILE_TEXT])[0]
    subject = profile()

    scored = [
        (
            score_posting(posting(title=title, description=body), subject, me, embedder).score,
            wanted,
            title,
        )
        for wanted, title, body in HAND_LABELED
    ]
    scored.sort(reverse=True)

    top_10 = scored[:10]
    hits = sum(1 for _, wanted, _ in top_10 if wanted)

    assert hits == 10, (
        "every posting worth applying to should rank in the top half; got "
        + ", ".join(f"{title}({score:.3f})" for score, _, title in top_10)
    )


def test_gate5_ranking_is_stable() -> None:
    """A feed that reshuffles between runs is one you cannot trust."""
    embedder = LexicalEmbedder()
    me = embedder.encode([PROFILE_TEXT])[0]
    subject = profile()

    def rank() -> list[str]:
        scored = [
            (
                score_posting(posting(title=t, description=b), subject, me, embedder).score,
                t,
            )
            for _, t, b in HAND_LABELED
        ]
        scored.sort(reverse=True)
        return [t for _, t in scored]

    assert rank() == rank()


# --------------------------------------------------------------------------
# The gap report — what the posting wants that the profile does not evidence
# --------------------------------------------------------------------------

_GAP_POSTING = """We are looking for an engineer to join our platform team.
You will build and operate Kubernetes clusters at scale. Strong Terraform
experience is required; Terraform is central to this role. Familiarity with Go
is a plus, and we write most services in Go. Experience with Prometheus
monitoring. We offer great benefits, medical dental vision, and equity."""


def _gap_posting() -> Posting:
    return Posting(
        id=uuid.uuid4(),
        title="Senior Kubernetes Platform Engineer",
        description_raw=_GAP_POSTING,
    )


def test_missing_terms_surfaces_the_real_gaps() -> None:
    """§2.1 stops the tailorer inventing a skill, which leaves the owner with
    a refusal and no information. This is the information."""
    gaps = missing_terms(
        "Senior backend engineer. Python, PostgreSQL, Docker, AWS.", _gap_posting()
    )

    assert "kubernetes" in gaps
    assert "terraform" in gaps


def test_missing_terms_excludes_boilerplate() -> None:
    """A gap list full of 'benefits' and 'team' is one nobody reads."""
    gaps = missing_terms("Python developer.", _gap_posting())

    for noise in ("benefits", "medical", "dental", "equity", "team", "great"):
        assert noise not in gaps


def test_a_term_the_profile_already_has_is_not_missing() -> None:
    profile = "Platform engineer. Kubernetes, Terraform, Go, Prometheus, Python."

    assert missing_terms(profile, _gap_posting()) == []


def test_a_named_tool_mentioned_once_still_counts() -> None:
    """Most postings say 'experience with Prometheus' exactly once. Requiring
    repetition would drop precisely the specific requirements."""
    gaps = missing_terms("Backend engineer. Python and Django.", _gap_posting())

    assert "prometheus" in gaps


def test_matched_and_missing_are_disjoint() -> None:
    profile = "Backend engineer. Python, Kubernetes, PostgreSQL."
    posting = _gap_posting()

    assert not set(keyword_overlap(profile, posting)) & set(missing_terms(profile, posting))


def test_a_trailing_full_stop_does_not_split_a_term() -> None:
    """'Go.' and 'Go' indexed as different tokens, which halved a term's count
    and put the same word in two buckets of the lexical embedding."""
    assert tokenize("we use Go. Go is fast") == ["use", "go", "go", "fast"]
    assert tokenize("built on node.js") == ["built", "node.js"]


def test_the_gap_report_reaches_the_match_feed() -> None:
    """A report nobody can see is not a report."""
    result = ScoredPosting(
        posting_id="x", score=0.5, matched_terms=["python"], missing_terms=["kubernetes"]
    )

    assert result.reasons()["missing_terms"] == ["kubernetes"]
    assert result.reasons()["matched_terms"] == ["python"]
