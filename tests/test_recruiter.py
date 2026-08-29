"""The recruiter axis — §17, §24, §52.

The point of a second evaluator is that it can disagree with the first. Most
of these tests are about that disagreement: a document that games the ATS must
lose here, or the axis is decoration.
"""

from __future__ import annotations

import inspect

import pytest

from packages.tailor import ats
from packages.tailor.parse import parse_text
from packages.tailor.recruiter import Shortlist, score

RESUME = """\
Dana Whitfield
dana@example.com | Austin, TX

EXPERIENCE

Staff Engineer, Analytical Engines Ltd
Mar 2021 - Present
- Built async APIs with FastAPI, deployed on Kubernetes and Docker.
- Migrated the billing service from MySQL to PostgreSQL with zero downtime.
- Reduced report generation time from 40 minutes to under 5 minutes.

Senior Software Engineer, Cartwright Data
Jun 2017 - Feb 2021
- Wrote Python services that processed 3 million customer events per day.

SKILLS
Python, PostgreSQL, FastAPI, Docker, Kubernetes
"""

POSTING = """\
Backend Engineer

We are a company that believes in people and in solving the world's problems
together. Our teams care deeply about the work and about each other.

What We Value
Strong experience with Python and PostgreSQL in production environments.
Familiarity with Docker, Kubernetes and async APIs at meaningful scale.
A track record of measurable improvements to system performance.
Comfort owning a service end to end, from schema design through deployment.
Experience migrating data stores without downtime is particularly welcome.
"""


# --------------------------------------------------------------------------
# §45 — the evaluator must not know whose side it is on
# --------------------------------------------------------------------------


def test_the_scorer_cannot_tell_an_original_from_a_rewrite() -> None:
    """Structural, not behavioural: there is no parameter that could say.

    An evaluator that knows it is looking at the optimizer's output is an
    optimizer grading itself, which is the objection §45 raises. A before/after
    pair is two independent calls and nothing distinguishes them.
    """
    parameters = set(inspect.signature(score).parameters)

    assert parameters == {"resume", "job_description"}


def test_scoring_is_deterministic() -> None:
    resume = parse_text(RESUME)

    assert score(resume, POSTING) == score(resume, POSTING)


# --------------------------------------------------------------------------
# The acceptance criterion: stuffing must cost more than it gains
# --------------------------------------------------------------------------

#: Same employers, same dates, no new claims — the posting's vocabulary packed
#: into every bullet. What an optimizer tuned only on keyword coverage drifts
#: toward.
STUFFED = """\
Dana Whitfield
dana@example.com | Austin, TX

EXPERIENCE

Staff Engineer, Analytical Engines Ltd
Mar 2021 - Present
- Python PostgreSQL Docker Kubernetes async APIs production performance.
- Python PostgreSQL production Docker Kubernetes async APIs performance.
- Python PostgreSQL Docker Kubernetes async production APIs performance.

Senior Software Engineer, Cartwright Data
Jun 2017 - Feb 2021
- Python PostgreSQL Docker Kubernetes async APIs production performance.

SKILLS
Python, PostgreSQL, FastAPI, Docker, Kubernetes
"""


def test_keyword_stuffing_scores_worse_than_the_honest_document() -> None:
    """The whole reason this module exists."""
    honest = score(parse_text(RESUME), POSTING)
    stuffed = score(parse_text(STUFFED), POSTING)

    assert stuffed.overall < honest.overall


def test_the_ats_prefers_the_stuffed_document_and_the_recruiter_does_not() -> None:
    """The disagreement, asserted.

    If these two ever move together on this pair, one of them has stopped
    measuring its own question and the second referee is worthless.
    """
    honest_resume, stuffed_resume = parse_text(RESUME), parse_text(STUFFED)

    assert ats.score(stuffed_resume, POSTING).keywords >= ats.score(honest_resume, POSTING).keywords
    assert score(stuffed_resume, POSTING).overall < score(honest_resume, POSTING).overall


def test_stuffing_is_reported_not_merely_scored() -> None:
    codes = {f.code for f in score(parse_text(STUFFED), POSTING).findings}

    assert "keyword_dense" in codes or "term_repeated" in codes


# --------------------------------------------------------------------------
# Credibility — the level that ends candidacies
# --------------------------------------------------------------------------


def test_a_skill_the_experience_never_shows_is_flagged() -> None:
    """ "It says Kubernetes but I cannot see where they used it"."""
    inflated = RESUME.replace(
        "Python, PostgreSQL, FastAPI, Docker, Kubernetes",
        "Python, PostgreSQL, FastAPI, Docker, Kubernetes, Rust, Kafka, Terraform, Elixir",
    )

    report = score(parse_text(inflated), POSTING)
    finding = next(f for f in report.findings if f.code == "unevidenced_skills")

    assert "Rust" in finding.detail
    assert "Kafka" in finding.detail
    assert report.credibility < score(parse_text(RESUME), POSTING).credibility


def test_skills_are_matched_after_whitespace_is_stripped() -> None:
    """A regression with a real cost.

    `normalize` lowercases and strips punctuation but not surrounding
    whitespace, so every skill after the first comma arrived as " fastapi" and
    matched nothing. On a résumé that evidences most of its skills this read as
    nine of ten unevidenced — a credibility score of 0.10 that was entirely an
    artifact of the split.
    """
    report = score(parse_text(RESUME), POSTING)

    assert report.credibility >= 0.9, "skills evidenced in the experience must count"


def test_a_resume_with_no_skills_section_is_not_punished() -> None:
    """Plenty of good résumés have none; scoring the format is not the job."""
    without = RESUME.split("SKILLS")[0]

    assert score(parse_text(without), POSTING).credibility == 1.0


# --------------------------------------------------------------------------
# The other levels
# --------------------------------------------------------------------------


def test_measurable_results_beat_responsibilities() -> None:
    vague = RESUME.replace(
        "- Reduced report generation time from 40 minutes to under 5 minutes.",
        "- Responsible for reporting performance and related improvements.",
    ).replace(
        "- Wrote Python services that processed 3 million customer events per day.",
        "- Responsible for Python services handling customer events.",
    )

    assert (
        score(parse_text(vague), POSTING).qualification
        < score(parse_text(RESUME), POSTING).qualification
    )


def test_burying_the_relevant_experience_costs_the_scan() -> None:
    """The ten-second pass. Same facts, further down the page."""
    padding = "\n".join(f"- Attended the {n}th weekly planning meeting." for n in range(12))
    buried = RESUME.replace("EXPERIENCE\n", f"EXPERIENCE\n\nEarly Career, Somewhere\n{padding}\n\n")

    assert score(parse_text(buried), POSTING).scan <= score(parse_text(RESUME), POSTING).scan


def test_an_overlong_bullet_is_reported() -> None:
    long_line = "- " + "Built and maintained a very large number of services and pipelines " * 5
    padded = RESUME.replace(
        "- Built async APIs with FastAPI, deployed on Kubernetes and Docker.", long_line
    )

    assert "bullet_too_long" in {f.code for f in score(parse_text(padded), POSTING).findings}


def test_without_a_posting_only_the_posting_free_levels_are_scored() -> None:
    """`scan` and `technical` ask "relative to this job". With no job, saying
    0.0 would read as a failure rather than a question not asked."""
    report = score(parse_text(RESUME))

    assert report.scored_against_posting is False
    assert report.scan == 0.0
    assert report.technical == 0.0
    assert report.qualification > 0.0
    assert report.credibility > 0.0


def test_a_resume_with_nothing_relevant_is_reported() -> None:
    unrelated = """\
Dana Whitfield
dana@example.com

EXPERIENCE

Pastry Chef, Corner Bakery
- Laminated dough for 200 croissants each morning.

SKILLS
Baking
"""
    report = score(parse_text(unrelated), POSTING)

    assert report.scan == 0.0
    assert "nothing_relevant_on_top" in {f.code for f in report.findings}


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def test_the_levels_are_never_hidden_behind_the_overall() -> None:
    report = score(parse_text(RESUME), POSTING)
    text = report.summary()

    for level in ("scan", "qualification", "credibility", "technical"):
        assert level in text


def test_the_shortlist_bands_are_ordered() -> None:
    honest = score(parse_text(RESUME), POSTING)
    stuffed = score(parse_text(STUFFED), POSTING)
    order = [
        Shortlist.STRONG_NO,
        Shortlist.NO,
        Shortlist.MAYBE,
        Shortlist.YES,
        Shortlist.STRONG_YES,
    ]

    assert order.index(honest.shortlist) >= order.index(stuffed.shortlist)


@pytest.mark.parametrize("level", ["scan", "qualification", "credibility", "technical"])
def test_every_level_stays_in_range(level: str) -> None:
    for text in (RESUME, STUFFED):
        assert 0.0 <= getattr(score(parse_text(text), POSTING), level) <= 1.0
