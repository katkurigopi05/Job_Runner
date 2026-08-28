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
    """The defect that mis-filed the owner's certifications under Skills."""
    text = RESUME.replace("SKILLS", "CERTIFICATIONS & TRAINING")
    report = score(parse_text(text))

    findings = [f for f in report.findings if f.code == "unrecognized_heading"]
    assert len(findings) == 1
    assert findings[0].line == "CERTIFICATIONS & TRAINING"


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
