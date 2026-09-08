"""Choosing which résumé to tailor from.

Every path read `profile.base_resume_id` and nothing else. Upload a backend
résumé, a data one and an ML one and two of them are unreachable — the
application always starts from whichever the profile happens to point at.

Silent, too: nothing fails, nothing is logged, and the tailorer does its whole
job on the wrong document. The employer receives a competent ML résumé for a
backend role.
"""

from __future__ import annotations

import uuid

import pytest

from packages.core.models import Candidate, Posting, Profile, Resume, User
from packages.matching.embed import LexicalEmbedder
from packages.matching.pick_resume import MIN_MARGIN, base_resumes, choose_base_resume
from packages.tailor.parse import parse_text

pytestmark = pytest.mark.asyncio

BACKEND = [
    "Senior Backend Engineer.",
    "Python, PostgreSQL, Django, REST APIs, Kubernetes, Docker, Redis.",
    "Built distributed services and database schemas at scale.",
]
ML = [
    "Machine Learning Engineer.",
    "PyTorch, TensorFlow, scikit-learn, transformers, embeddings, GPU training.",
    "Trained and served deep learning models for ranking and recommendation.",
]
DATA = [
    "Data Engineer.",
    "Airflow, dbt, Snowflake, Spark, ETL pipelines, data warehousing.",
    "Built batch and streaming pipelines feeding analytics.",
]


async def _candidate(session) -> Candidate:
    suffix = uuid.uuid4().hex[:8]
    user = User(email=f"o-{suffix}@example.com")
    session.add(user)
    await session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email=f"c-{suffix}@example.com")
    session.add(candidate)
    await session.flush()
    return candidate


async def _resume(session, candidate: Candidate, lines: list[str], version: int) -> Resume:
    # Parsed the way an upload is, not hand-built from `raw_lines`. A row
    # carrying only raw lines has no sections and no contact, so `ats.score`
    # reads every fixture as equally unparseable — which silently disabled the
    # sendability gate in `test_an_unreadable_resume_cannot_win_on_similarity`
    # and made it assert the opposite of what it says.
    resume = Resume(
        candidate_id=candidate.id,
        version=version,
        storage_ref=f"resumes/{candidate.id}/v{version}/resume.pdf",
        parsed_json=parse_text("\n".join(lines)).model_dump(),
    )
    session.add(resume)
    await session.flush()
    return resume


def _profile(candidate: Candidate, base: Resume | None) -> Profile:
    return Profile(
        id=uuid.uuid4(),
        candidate_id=candidate.id,
        label="owner",
        base_resume_id=base.id if base else None,
        links_json={},
        answers_kv_json={},
    )


def _posting(title: str, body: str) -> Posting:
    return Posting(
        id=uuid.uuid4(),
        url=f"https://boards.greenhouse.io/acme/jobs/{uuid.uuid4().hex[:6]}",
        title=title,
        description_raw=body,
        location="Remote - US",
    )


def _text(title: str, body: str) -> str:
    """What the apply pipeline hands the selector: a parsed page's text."""
    return f"{title}\n{body}"


async def test_the_backlogs_case_three_resumes_two_unreachable(db_session) -> None:
    """The complaint P2 was written for, with the profile pointing at the ML one."""
    candidate = await _candidate(db_session)
    backend = await _resume(db_session, candidate, BACKEND, 1)
    ml = await _resume(db_session, candidate, ML, 2)
    await _resume(db_session, candidate, DATA, 3)
    profile = _profile(candidate, base=ml)

    choice = await choose_base_resume(
        db_session,
        profile,
        _text(
            "Senior Backend Engineer",
            "Python, PostgreSQL, Django and Kubernetes. REST APIs, Redis, Docker.",
        ),
        embedder=LexicalEmbedder(),
    )

    assert choice is not None
    assert choice.resume_id == backend.id, "the backend posting must pick the backend résumé"
    assert choice.resume_id != profile.base_resume_id, "and not the profile's standing pick"
    assert len(choice.considered) == 3


async def test_an_ml_posting_picks_the_ml_resume(db_session) -> None:
    """The mirror, so the test is not just asserting a fixed answer."""
    candidate = await _candidate(db_session)
    backend = await _resume(db_session, candidate, BACKEND, 1)
    ml = await _resume(db_session, candidate, ML, 2)
    profile = _profile(candidate, base=backend)

    choice = await choose_base_resume(
        db_session,
        profile,
        _text(
            "Machine Learning Engineer",
            "PyTorch, transformers and embeddings. Train and serve deep learning models.",
        ),
        embedder=LexicalEmbedder(),
    )

    assert choice is not None
    assert choice.resume_id == ml.id


async def test_a_resume_tailored_for_another_posting_is_never_chosen(db_session) -> None:
    """The one selection that would be actively harmful.

    A tailored résumé is already bent toward a different job. It lives in the
    same table, so excluding it is not automatic — `tailored_for_posting_id`
    is the discriminator, set on every tailored row including the uncacheable
    ones where `tailored_key` is NULL.
    """
    candidate = await _candidate(db_session)
    await _resume(db_session, candidate, BACKEND, 1)

    # A real row: `tailored_for_posting_id` carries a foreign key, which is
    # itself part of the guarantee — a tailored résumé always names a posting
    # that exists, so it can never look like an upload by accident.
    other = _posting("Some Other Role", "Something else entirely.")
    db_session.add(other)
    await db_session.flush()

    tailored = await _resume(db_session, candidate, BACKEND, 2)
    tailored.tailored_for_posting_id = other.id
    await db_session.flush()

    available = await base_resumes(db_session, candidate.id)

    assert tailored.id not in {r.id for r in available}
    assert len(available) == 1


async def test_a_single_resume_needs_no_scoring(db_session) -> None:
    candidate = await _candidate(db_session)
    only = await _resume(db_session, candidate, BACKEND, 1)
    profile = _profile(candidate, base=only)

    choice = await choose_base_resume(
        db_session, profile, _text("Anything", "Anything at all."), embedder=LexicalEmbedder()
    )

    assert choice is not None
    assert choice.resume_id == only.id
    assert "only base résumé" in choice.reason


async def test_no_usable_resume_means_carry_on_as_before(db_session) -> None:
    """None is "keep whatever the profile said", not "fail"."""
    candidate = await _candidate(db_session)
    profile = _profile(candidate, base=None)

    assert (
        await choose_base_resume(
            db_session, profile, _text("Backend", "Python."), embedder=LexicalEmbedder()
        )
        is None
    )


async def test_a_resume_with_no_text_is_not_a_candidate(db_session) -> None:
    """An unparsed upload would score 0 and win nothing, but it must not be
    offered as though it were a real document."""
    candidate = await _candidate(db_session)
    good = await _resume(db_session, candidate, BACKEND, 1)
    empty = await _resume(db_session, candidate, [], 2)

    available = {r.id for r in await base_resumes(db_session, candidate.id)}

    assert good.id in available
    assert empty.id not in available


async def test_a_margin_inside_the_noise_keeps_the_profiles_own_choice(db_session) -> None:
    """Two near-identical résumés must not flip-flop between runs.

    Which document an employer receives should not turn on a cosine
    difference of a few thousandths.
    """
    candidate = await _candidate(db_session)
    first = await _resume(db_session, candidate, BACKEND, 1)
    await _resume(db_session, candidate, [*BACKEND, "Also mentored two engineers."], 2)
    profile = _profile(candidate, base=first)

    choice = await choose_base_resume(
        db_session,
        profile,
        _text("Senior Backend Engineer", "Python, PostgreSQL, Django, Kubernetes."),
        embedder=LexicalEmbedder(),
    )

    assert choice is not None
    scores = [score for _, score in choice.considered]
    if scores[0] - scores[1] < MIN_MARGIN:
        assert choice.resume_id == first.id
        assert "inside the noise" in choice.reason


async def test_the_choice_records_its_runners_up(db_session) -> None:
    """A selection with no alternatives shown is unauditable on /review."""
    candidate = await _candidate(db_session)
    await _resume(db_session, candidate, BACKEND, 1)
    await _resume(db_session, candidate, ML, 2)
    profile = _profile(candidate, base=None)

    choice = await choose_base_resume(
        db_session,
        profile,
        _text("Backend Engineer", "Python and PostgreSQL."),
        embedder=LexicalEmbedder(),
    )

    assert choice is not None
    rendered = choice.as_dict()
    assert len(rendered["considered"]) == 2
    assert {"version", "score"} <= set(rendered["considered"][0])
    assert rendered["reason"]


# --- sendability gates similarity ---------------------------------------------

#: Parses at 100%: contact links, a recognised education section, real dates.
COMPLETE = [
    "Gopi Krishna Reddy",
    "(925) 555-0100 | g@example.com | linkedin.com/in/gopi | github.com/gopi",
    "PROFESSIONAL SUMMARY",
    "Machine learning engineer with six years building models.",
    "EDUCATION",
    "State University, Hayward, CA",
    "Bachelor of Science in Computer Science, Aug 2016 - May 2020",
    "EXPERIENCE",
    "Machine Learning Engineer, Acme Corp, Jan 2021 - Dec 2024",
    "Trained and served PyTorch ranking models on GPU clusters.",
    "SKILLS",
    "PyTorch, TensorFlow, scikit-learn, transformers, embeddings",
]

#: Parses at 56%: no links, no education an ATS recognises, no dates. The
#: shape of the stub sitting in the owner's own database as v1.
STUB = [
    "Gopi K",
    "g@example.com | +1-555-0142",
    "Summary",
    "Backend engineer.",
    "Experience",
    "Senior Engineer, Example Corp",
    "Skills",
    "Python, PostgreSQL, Django, Kubernetes, Docker, Redis",
]


async def test_an_unreadable_resume_cannot_win_on_similarity(db_session) -> None:
    """The defect this gate exists for, from the owner's real data.

    An eight-line stub — `Backend engineer.`, `Senior Engineer, Example Corp`,
    a 555 phone number — beat their real résumé 0.642 to 0.609 on a support
    posting and would have been uploaded to the employer. The margin was
    outside `MIN_MARGIN`, so the tie-break correctly did not fire: nothing was
    broken, the question was simply never asked.

    The posting here is deliberately *backend*, so the stub is genuinely the
    closer match on wording. It must still lose, because a résumé an ATS reads
    at 56% scores nothing with the employer however well it matches.
    """
    candidate = await _candidate(db_session)
    stub = await _resume(db_session, candidate, STUB, 1)
    complete = await _resume(db_session, candidate, COMPLETE, 2)
    profile = _profile(candidate, base=None)

    choice = await choose_base_resume(
        db_session,
        profile,
        _text("Senior Backend Engineer", "Python, PostgreSQL, Django, Kubernetes, Docker."),
        embedder=LexicalEmbedder(),
    )

    assert choice is not None
    assert choice.resume_id == complete.id, "the readable résumé must win"
    assert choice.resume_id != stub.id
    assert "56%" in choice.reason, "and the reason must name what was dropped and why"


async def test_similarity_still_decides_between_readable_resumes(db_session) -> None:
    """The gate must not swallow the feature it was added to.

    Two équally readable résumés are still chosen between on the merits — the
    floor is for "not fit to send", not a preference for tidier formatting.
    """
    candidate = await _candidate(db_session)
    ml = await _resume(db_session, candidate, COMPLETE, 1)
    backend = await _resume(
        db_session,
        candidate,
        [
            *COMPLETE[:8],
            "Senior Backend Engineer, Acme Corp, Jan 2021 - Dec 2024",
            "Built distributed Python services on PostgreSQL and Kubernetes.",
            "SKILLS",
            "Python, PostgreSQL, Django, Kubernetes, Docker, Redis",
        ],
        2,
    )
    profile = _profile(candidate, base=None)

    choice = await choose_base_resume(
        db_session,
        profile,
        _text("Senior Backend Engineer", "Python, PostgreSQL, Django, Kubernetes, Docker, Redis."),
        embedder=LexicalEmbedder(),
    )

    assert choice is not None
    assert choice.resume_id == backend.id
    assert choice.resume_id != ml.id


async def test_one_resume_is_never_gated_away(db_session) -> None:
    """A single stub is still the document. Refusing would send none at all.

    §15 already settles the direction: an honest untailored résumé beats an
    application with no résumé.
    """
    candidate = await _candidate(db_session)
    stub = await _resume(db_session, candidate, STUB, 1)
    profile = _profile(candidate, base=stub)

    choice = await choose_base_resume(
        db_session, profile, _text("Backend Engineer", "Python."), embedder=LexicalEmbedder()
    )

    assert choice is not None
    assert choice.resume_id == stub.id


async def test_uniformly_poor_resumes_still_yield_a_pick(db_session) -> None:
    """Relative, not a fixed bar.

    An owner whose résumés all parse badly should still get the best match
    among them rather than an empty hand.
    """
    candidate = await _candidate(db_session)
    await _resume(db_session, candidate, BACKEND, 1)
    await _resume(db_session, candidate, ML, 2)
    profile = _profile(candidate, base=None)

    choice = await choose_base_resume(
        db_session,
        profile,
        _text("Machine Learning Engineer", "PyTorch, transformers, embeddings, GPU training."),
        embedder=LexicalEmbedder(),
    )

    assert choice is not None
    assert len(choice.considered) == 2, "both stayed in the running"
