"""The ATS score measures the document, not the rewriter's opinion of itself.

Every case here is drawn from a defect observed in the owner's own résumé —
`CERTIFICATIONS & TRAINING` filed as content, `AnalyticsJan 2025` with the tab
gone — or from a false positive the first version produced on it.
"""

from __future__ import annotations

from packages.tailor.ats import (
    MAX_BULLET_CHARS,
    AtsReport,
    Finding,
    _deduction,
    _looks_like_heading,
    _requirements_text,
    compare,
    score,
)
from packages.tailor.guard import SourceCorpus
from packages.tailor.keywords import _supported
from packages.tailor.parse import parse_text

RESUME = """\
Jane Doe
jane@example.com | (555) 123-4567 | github.com/janedoe

SUMMARY
Backend engineer who builds data pipelines.

EXPERIENCE
Acme Corp, San Francisco, CA
Software Engineer, Jan 2021 - Present
Built payment services in Python and Postgres.

EDUCATION
State University, Bachelor of Science in Computer Science, Aug 2016 - May 2020

SKILLS
Python, PostgreSQL, Kubernetes
"""


def _codes(report: AtsReport) -> list[str]:
    return [f.code for f in report.findings]


# --------------------------------------------------------------------------
# Parse half
# --------------------------------------------------------------------------


def test_a_well_formed_resume_has_no_findings() -> None:
    report = score(parse_text(RESUME))
    assert report.findings == []
    assert report.parse == 1.0


def test_an_unrecognized_heading_is_reported_with_the_line() -> None:
    """A heading the parser has no pattern for still has to be reported.

    This used to use `CERTIFICATIONS & TRAINING`, which the parser now matches.
    That is the fix working, not the check becoming unnecessary: the point of
    the finding is that whatever the parser cannot place, the owner is told
    about rather than left to wonder why a section is empty.
    """
    text = RESUME.replace("SKILLS", "VOLUNTEER WORK")
    report = score(parse_text(text))

    findings = [f for f in report.findings if f.code == "unrecognized_heading"]
    assert len(findings) == 1
    assert findings[0].line == "VOLUNTEER WORK"


def test_a_compound_heading_is_no_longer_reported_as_unrecognized() -> None:
    """`CERTIFICATIONS & TRAINING` and `ACHIEVEMENTS & ACTIVITIES` now parse."""
    for heading in ("CERTIFICATIONS & TRAINING", "ACHIEVEMENTS & ACTIVITIES"):
        report = score(parse_text(RESUME.replace("SKILLS", heading)))
        assert "unrecognized_heading" not in _codes(report), heading


def test_a_tech_stack_line_is_not_reported_as_a_heading() -> None:
    """The first version flagged ten of these and buried the two real ones."""
    text = RESUME.replace(
        "Built payment services in Python and Postgres.",
        "Python, FastAPI, React, HuggingFace Transformers, Qdrant",
    )
    report = score(parse_text(text))
    assert "unrecognized_heading" not in _codes(report)


def test_a_project_title_is_not_reported_as_a_heading() -> None:
    for title in (
        "Attorney.AI — Citation-First Legal Research RAG Assistant   [GitHub]",
        "California Fire Incident Classification   [GitHub]",
        "Machine Learning with Python — Inmovidu Tech",
        "California State University East Bay, Hayward, CA",
    ):
        assert not _looks_like_heading(title), title


def test_real_section_headings_are_recognized_as_heading_shaped() -> None:
    for heading in ("CERTIFICATIONS & TRAINING", "ACHIEVEMENTS & ACTIVITIES", "PROJECTS"):
        assert _looks_like_heading(heading), heading


def test_a_date_fused_to_the_word_before_it_is_reported() -> None:
    """`Business AnalyticsJan 2025` — the residue of a dropped tab."""
    text = RESUME.replace(
        "Bachelor of Science in Computer Science, Aug 2016 - May 2020",
        "Bachelor of Science in Computer ScienceAug 2016 - May 2020",
    )
    report = score(parse_text(text))
    assert "fused_date" in _codes(report)


def test_camel_case_technology_names_are_not_reported_as_fused_dates() -> None:
    text = RESUME.replace(
        "Built payment services in Python and Postgres.",
        "Built services with HuggingFace, BigQuery, WebGPU, XGBoost and OpenCV.",
    )
    report = score(parse_text(text))
    assert "fused_date" not in _codes(report)


def test_a_missing_email_costs_more_than_a_missing_phone() -> None:
    no_email = score(parse_text(RESUME.replace("jane@example.com | ", "")))
    no_phone = score(parse_text(RESUME.replace("(555) 123-4567 | ", "")))
    assert no_email.parse < no_phone.parse


def test_projects_substitutes_for_experience() -> None:
    """The owner's résumé has no employment section, and that is not a defect."""
    text = RESUME.replace("EXPERIENCE", "PROJECTS")
    report = score(parse_text(text))
    assert not [
        f for f in report.findings if f.code == "missing_section" and "experience" in f.detail
    ]


def test_a_resume_with_neither_experience_nor_projects_is_reported() -> None:
    text = RESUME.replace("EXPERIENCE", "INTERESTS")
    report = score(parse_text(text))
    assert "missing_section" in _codes(report)


def test_an_overlong_bullet_is_reported_but_a_long_summary_is_not() -> None:
    long_line = "Built " + "and shipped services " * 20
    assert len(long_line) > MAX_BULLET_CHARS

    in_summary = score(
        parse_text(RESUME.replace("Backend engineer who builds data pipelines.", long_line))
    )
    assert "overlong_bullet" not in _codes(in_summary)

    in_experience = score(
        parse_text(RESUME.replace("Built payment services in Python and Postgres.", long_line))
    )
    assert "overlong_bullet" in _codes(in_experience)


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def test_repeats_of_one_defect_cost_progressively_less() -> None:
    """Ten overlong bullets are one problem, not ten.

    Summing flat floored the owner's résumé at 0%, where no fix could move the
    number until the last one.
    """
    one = _deduction([Finding("x", "", 0.2)])
    four = _deduction([Finding("x", "", 0.2) for _ in range(4)])
    assert one == 0.2
    assert four < 0.8
    assert four > one


def test_a_deduction_never_drives_the_score_below_zero() -> None:
    findings = [Finding(f"c{i}", "", 0.9) for i in range(5)]
    assert _deduction(findings) > 1.0
    report = AtsReport(parse=max(0.0, 1.0 - _deduction(findings)), keywords=0.0)
    assert report.parse == 0.0


def test_no_posting_means_the_keyword_half_is_not_reported() -> None:
    report = score(parse_text(RESUME))
    assert report.scored_against_posting is False
    # 0.0 here means "not asked", and `overall` must not read it as "matched
    # nothing" — a parse-only score is the parse score.
    assert report.overall == report.parse


def test_the_keyword_half_is_computed_when_a_posting_is_given() -> None:
    posting = "We need a backend engineer with Python, PostgreSQL and Kubernetes."
    report = score(parse_text(RESUME), posting)
    assert report.scored_against_posting is True
    assert report.keywords > 0.0


def test_an_alias_counts_as_support() -> None:
    """The résumé says Postgres; the posting says PostgreSQL."""
    resume = parse_text(RESUME.replace("Python, PostgreSQL, Kubernetes", "Python, Postgres, K8s"))
    report = score(resume, "PostgreSQL and Kubernetes experience required. PostgreSQL. Kubernetes.")
    joined = " ".join(report.supported).lower()
    assert "postgresql" in joined or "kubernetes" in joined


# --------------------------------------------------------------------------
# Posting text
# --------------------------------------------------------------------------


def test_the_legal_footer_is_cut_before_terms_are_counted() -> None:
    body = "We need Python and Kubernetes. " * 20
    footer = "We are an equal opportunity employer. " + (
        "All qualified applicants receive consideration regardless of gender. " * 10
    )
    trimmed = _requirements_text(body + footer)
    assert "equal opportunity" not in trimmed.lower()
    assert "Kubernetes" in trimmed


def test_a_posting_that_is_mostly_footer_is_kept_whole() -> None:
    """Cutting to almost nothing means we misread it, not that it is all legal."""
    text = "Equal opportunity employer. " + ("We value everyone. " * 50)
    assert _requirements_text(text) == text


def test_a_posting_with_no_footer_is_untouched() -> None:
    text = "Senior backend engineer. Python, Postgres, Kubernetes."
    assert _requirements_text(text) == text


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def test_compare_shows_both_halves_so_a_trade_off_cannot_hide() -> None:
    before = AtsReport(parse=0.9, keywords=0.2, scored_against_posting=True)
    after = AtsReport(parse=0.5, keywords=0.8, scored_against_posting=True)
    line = compare(before, after)
    assert "90% → 50%" in line
    assert "20% → 80%" in line


# --------------------------------------------------------------------------
# Before / after
# --------------------------------------------------------------------------


def test_score_change_reports_both_halves_and_what_was_gained() -> None:
    from packages.tailor.ats import score_change

    posting = "We need Kubernetes and PostgreSQL experience. Kubernetes. PostgreSQL. Kafka."
    # The term has to be absent from the *whole* source, not just from the
    # bullet: an earlier fixture only edited the bullet and left `Kubernetes`
    # in the skills line, so both sides supported it and `gained` was empty.
    before = parse_text(RESUME.replace("Python, PostgreSQL, Kubernetes", "Python"))
    after = parse_text(RESUME)

    delta = score_change(before, after, posting)

    assert delta.parse_before > 0
    assert delta.keywords_after >= delta.keywords_before
    assert delta.gained

    # Nothing invented: every gained term is one the résumé actually backs.
    #
    # Asserted against the corpus rather than against the raw text, because an
    # alias is a legitimate gain — a posting asking for `PostgreSQL` is backed
    # by a résumé that says `Postgres`, and the term would not appear verbatim.
    # An earlier version compared each term against `" ".join(delta.gained)`,
    # which always contains it, so the property went untested.
    corpus = SourceCorpus.from_resume(after)
    for term in delta.gained:
        assert _supported(term, corpus), term


def test_a_parse_regression_is_reported_rather_than_averaged_away() -> None:
    """The failure mode the pair exists to catch."""
    from packages.tailor.ats import AtsDelta

    delta = AtsDelta(parse_before=0.9, parse_after=0.5, keywords_before=0.2, keywords_after=0.8)
    assert delta.parse_regressed


def test_no_regression_when_parse_holds() -> None:
    from packages.tailor.ats import AtsDelta

    delta = AtsDelta(parse_before=0.9, parse_after=0.9, keywords_before=0.2, keywords_after=0.4)
    assert not delta.parse_regressed


def test_still_missing_terms_are_carried_so_the_owner_can_judge_fit() -> None:
    from packages.tailor.ats import score_change

    posting = "Requires Erlang, Erlang, Erlang and COBOL, COBOL, COBOL experience."
    delta = score_change(parse_text(RESUME), parse_text(RESUME), posting)
    joined = " ".join(delta.still_missing).lower()
    assert "erlang" in joined or "cobol" in joined


def test_a_name_containing_a_month_is_not_a_fused_date() -> None:
    """`Arjun` ends in `jun`; `Omar` ends in `mar`.

    The first version matched a month case-insensitively with nothing after it,
    so both were reported as fused dates and both cost their owner parse score
    for having a name.
    """
    for name in ("Arjun Kumar", "Omar Sharif", "Janet Maynard", "Julie Marchetti", "Maya Decker"):
        report = score(parse_text(RESUME.replace("Jane Doe", name)))
        assert "fused_date" not in _codes(report), name


def test_a_month_without_a_year_is_not_a_fused_date() -> None:
    text = RESUME.replace(
        "Built payment services in Python and Postgres.", "Shipped the MarTech integration."
    )
    assert "fused_date" not in _codes(score(parse_text(text)))
