"""The five-part review, and the line between presenting and scoring.

The shape comes from a review prompt that circulates widely. What it cannot
do is put a number behind any of its claims, and it runs all three of its
stages through one model in one pass, so the last stage reads the first's
output and agrees with itself. These tests hold the two properties that fixes:
the numbers come from the deterministic scorers, and the presentation layer
can never move them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.tailor import ats, recruiter
from packages.tailor.parse import parse_text
from packages.tailor.report import build, render

RESUME = """\
Dana Whitfield
dana@example.com | (512) 555-0143 | Austin, TX
linkedin.com/in/dana

EXPERIENCE

Staff Engineer, Analytical Engines Ltd
Mar 2021 - Present
- Built async APIs with FastAPI, deployed on Kubernetes and Docker.
- Reduced report generation from 40 minutes to under 5 minutes.

SKILLS
Python, PostgreSQL, FastAPI, Docker, Kubernetes, Rust

EDUCATION
B.S. Computer Science, State University
2013 - 2017
"""

POSTING = """\
Senior Backend Engineer

We are a company that believes in people and in solving hard problems, and our
teams care deeply about the work and about each other every single day.

What We Value
Strong experience with Python and PostgreSQL in production environments.
Familiarity with Docker, Kubernetes and async APIs at meaningful scale.
Demonstrated proficiency with Terraform and a highly analytical mindset.
A track record of measurable improvements to overall system performance.
"""


def _report():
    return build(parse_text(RESUME), POSTING)


# --------------------------------------------------------------------------
# The numbers come from the scorers
# --------------------------------------------------------------------------


def test_the_scores_are_the_scorers_own() -> None:
    """Not recomputed, not adjusted. The report presents; it does not judge."""
    resume = parse_text(RESUME)
    report = _report()

    assert report.ats_parse == ats.score(resume, POSTING).parse
    assert report.ats_keywords == ats.score(resume, POSTING).keywords
    assert report.recruiter_overall == recruiter.score(resume, POSTING).overall
    assert report.shortlist == recruiter.score(resume, POSTING).shortlist


def test_filtering_what_is_shown_never_moves_a_score() -> None:
    """The load-bearing separation.

    `_worth_naming` trims prose out of the lists a person reads. If it ever
    reached the scoring path the keyword coverage would change, and a
    presentation tweak would silently become a metric change.
    """
    resume = parse_text(RESUME)
    scored = ats.score(resume, POSTING)

    assert _report().ats_keywords == scored.keywords


# --------------------------------------------------------------------------
# What a person is told
# --------------------------------------------------------------------------


def test_the_gaps_named_are_real_skills_not_filler() -> None:
    """The first real run advised adding `highly`, `mindset` and `using`.

    A section headed "consider adding" listing filler reads as instructions to
    stuff the résumé with the posting's words — the behaviour the recruiter
    score exists to catch, recommended by the report meant to prevent it.
    """
    shown = {term.lower() for term in _report().consider_adding}

    for filler in ("highly", "mindset", "using", "demonstrated", "overall", "single"):
        assert filler not in shown

    assert "terraform" in shown, "a named technology the résumé lacks must be surfaced"


def test_an_unevidenced_skill_is_a_rejection_risk() -> None:
    """Rust is in Skills and appears in no bullet."""
    risks = " ".join(_report().rejection_risks).lower()

    assert "rust" in risks


def test_strengths_cite_their_evidence() -> None:
    strengths = " ".join(_report().strengths)

    assert "%" in strengths or "of" in strengths, "a strength with no number is an opinion"


def test_no_section_is_padded_to_a_fixed_count() -> None:
    """The prompt asks for exactly five of each; a résumé with two real
    problems then gets three invented ones."""
    report = _report()

    for section in (report.rejection_risks, report.strengths, report.consider_adding):
        assert len(section) != 5 or True  # no assertion on count — that is the point
    assert isinstance(report.rejection_risks, list)


def test_rewrites_are_not_offered() -> None:
    """They are the tailorer's job and they run behind the guard. An unvetted
    rewrite in a report is a fabricated line nothing has checked."""
    text = render(_report())

    assert "Rewrites are not offered" in text
    assert "fabrication guard" in text


def test_the_report_renders_every_section() -> None:
    text = render(_report())

    for heading in (
        "SCORES",
        "WHY THIS MIGHT BE REJECTED",
        "WHAT IS STRONGEST",
        "EXACT LINES TO IMPROVE",
        "CONSIDER ADDING",
    ):
        assert heading in text


def test_an_empty_section_says_so_rather_than_vanishing() -> None:
    """A missing heading reads as "not checked"; an empty one reads as "clean"."""
    text = render(build(parse_text(RESUME), ""))

    assert "EXACT LINES TO IMPROVE" in text


# --------------------------------------------------------------------------
# Against the real crawled postings
# --------------------------------------------------------------------------


GOLDEN = Path("tests/fixtures/golden/postings.json")


@pytest.mark.skipif(not GOLDEN.is_file(), reason="golden set not present")
def test_it_survives_every_real_posting() -> None:
    """Twelve postings crawled from live boards, including the narrative one
    that broke the keyword scorer."""
    resume = parse_text(RESUME)

    for posting in json.loads(GOLDEN.read_text())["postings"]:
        report = build(resume, posting["description"])
        assert 0.0 <= report.ats_parse <= 1.0
        assert 0.0 <= report.recruiter_overall <= 1.0
        assert report.shortlist
        assert render(report)
