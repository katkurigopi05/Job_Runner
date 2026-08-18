"""The fabrication merge gate — CLAUDE.md §2.1 and Gate 3.

Two halves, and both matter:

1. **The guard catches fabrication.** Adversarial outputs that add a skill,
   employer, date, credential, or metric must be rejected. A guard that never
   fires proves nothing.
2. **The guard does not cry wolf.** Legitimate rephrasing must pass. A guard
   that rejects everything would be trivially "safe" and would get switched
   off within a week.

Gate 3 asks for 20 job descriptions crossed with 3 résumés — 60 combinations,
every entity in the output traced to source. That is
`test_gate3_every_combination_is_clean`.

No test uses a network model. The stub is deterministic by construction.
"""

from __future__ import annotations

import pytest

from packages.llm.provider import StubProvider
from packages.tailor.diff import render_html, summarize, unified
from packages.tailor.guard import (
    EntityKind,
    FabricationError,
    SourceCorpus,
    check,
    check_or_raise,
    extract_entities,
    normalize,
)
from packages.tailor.parse import parse_text
from packages.tailor.rewrite import tailor_bullet, tailor_bullets, vet

# --------------------------------------------------------------------------
# Fixtures: 3 résumés, 20 job descriptions
# --------------------------------------------------------------------------

RESUME_BACKEND = """
Ada Lovelace
Staff Engineer, Analytical Engines Ltd, 2021 to present
Designed the note-taking subsystem handling 2M events per day.
Mentored four engineers through promotion.
Migrated the billing service from MySQL to PostgreSQL with zero downtime.
Skills: Python, PostgreSQL, Docker, Kubernetes, FastAPI
"""

RESUME_DATA = """
Grace Hopper
Data Engineer, Naval Systems Inc, 2019 to 2024
Built ingestion pipelines processing 40TB per month in Spark.
Cut warehouse query latency by 60% by redesigning the partition scheme.
Owned the Airflow deployment used by 30 analysts.
Skills: Python, Spark, Airflow, SQL, dbt
"""

RESUME_FRONTEND = """
Alan Turing
Frontend Engineer, Bletchley Labs, 2020 to 2023
Rebuilt the dashboard in React, cutting first paint to 800ms.
Introduced a component library adopted by three teams.
Improved accessibility to WCAG AA across the checkout flow.
Skills: TypeScript, React, Next.js, CSS, Playwright
"""

RESUMES = {
    "backend": RESUME_BACKEND,
    "data": RESUME_DATA,
    "frontend": RESUME_FRONTEND,
}

JOB_DESCRIPTIONS = [
    "Senior Backend Engineer working on distributed systems in Python.",
    "Platform Engineer. Kubernetes, Docker, infrastructure as code.",
    "Data Engineer to own our Spark pipelines and warehouse modelling.",
    "Full Stack Engineer. React front end, Python services behind it.",
    "Staff Engineer to lead architecture for a high-traffic API.",
    "Site Reliability Engineer. Latency, observability, on-call.",
    "Analytics Engineer working in dbt and SQL on a modern warehouse.",
    "Frontend Engineer specialising in accessibility and design systems.",
    "Backend Engineer, PostgreSQL heavy, event-driven architecture.",
    "Machine Learning Engineer to productionise models at scale.",
    "DevOps Engineer to own CI/CD and deployment tooling.",
    "Software Engineer, general backend, mentoring junior engineers.",
    "Principal Engineer to set technical direction across teams.",
    "Data Platform Engineer. Airflow orchestration, batch and streaming.",
    "React Engineer building a customer-facing dashboard.",
    "API Engineer designing and documenting public REST interfaces.",
    "Database Engineer focused on query performance and migrations.",
    "Test Engineer building browser automation with Playwright.",
    "Cloud Engineer. Containers, orchestration, cost optimisation.",
    "Engineering Manager, hands-on, growing a backend team.",
]


def bullets_of(resume: str) -> list[str]:
    """The claim-bearing lines of a fixture résumé."""
    return [
        line.strip()
        for line in resume.strip().splitlines()
        if line.strip() and not line.startswith("Skills:")
    ][2:]


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("Python", "python"),
        ("2,000", "2000"),
        ("40%", "40"),
        ("three", "3"),
        # Words are never mangled: de-pluralization is a separate matching
        # index, so nothing is ever reported back as "kubernete".
        ("Kubernetes.", "kubernetes"),
        ("engineers", "engineers"),
    ],
)
def test_normalize(token: str, expected: str) -> None:
    assert normalize(token) == expected


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("engineers", "engineer"),
        ("business", "business"),  # -ss must not lose its s
        ("analysis", "analysis"),  # -is likewise
        ("api", "api"),
    ],
)
def test_singular_matching_form(token: str, expected: str) -> None:
    from packages.tailor.guard import singular

    assert singular(token) == expected


def test_entity_kinds() -> None:
    entities = {e.text: e.kind for e in extract_entities("Built AWS pipelines in 2021 using 40TB")}
    assert entities["AWS"] is EntityKind.ACRONYM
    assert entities["2021"] is EntityKind.YEAR
    # The unit stays attached: 40TB is one claim, not "40" beside "TB".
    assert entities["40TB"] is EntityKind.NUMBER
    assert "Built" not in entities  # a common verb carries no claim


def test_common_words_are_not_proper_nouns() -> None:
    """Sentence casing must not read as a factual claim."""
    entities = extract_entities("Designed the system. Improved performance. Managed a team.")
    assert entities == []


# --------------------------------------------------------------------------
# The guard fires on real fabrication
# --------------------------------------------------------------------------


@pytest.fixture
def corpus() -> SourceCorpus:
    return SourceCorpus.from_texts(RESUME_BACKEND)


def test_invented_metric_is_caught(corpus) -> None:
    report = check("Designed the subsystem, improving throughput by 87%.", corpus)
    assert not report.ok
    assert any(v.entity.normalized == "87" for v in report.violations)


def test_invented_employer_is_caught(corpus) -> None:
    report = check("Staff Engineer at Google building systems.", corpus)
    assert not report.ok
    assert any(v.entity.text == "Google" for v in report.violations)


def test_invented_skill_is_caught(corpus) -> None:
    report = check("Designed the subsystem in Rust and Terraform.", corpus)
    assert not report.ok
    flagged = {v.entity.text for v in report.violations}
    assert {"Rust", "Terraform"} <= flagged


def test_invented_credential_is_caught(corpus) -> None:
    report = check("Designed the subsystem. AWS certified, holds a PMP.", corpus)
    assert not report.ok
    assert {"AWS", "PMP"} <= {v.entity.text for v in report.violations}


def test_invented_date_is_caught(corpus) -> None:
    report = check("Led the team from 2015 to present.", corpus)
    assert not report.ok
    assert any(v.entity.kind is EntityKind.YEAR for v in report.violations)


def test_inflated_metric_is_caught(corpus) -> None:
    """2M in the source does not license 20M in the output."""
    report = check("Handled 20M events per day.", corpus)
    assert not report.ok


def test_check_or_raise(corpus) -> None:
    with pytest.raises(FabricationError, match="unsupported"):
        check_or_raise("Built it in Rust.", corpus)


# --------------------------------------------------------------------------
# The guard permits legitimate rewriting
# --------------------------------------------------------------------------


def test_rephrasing_passes(corpus) -> None:
    assert check("Architected a note-taking subsystem processing 2M events daily.", corpus).ok


def test_reordering_passes(corpus) -> None:
    assert check(
        "Handling 2M events per day, the note-taking subsystem was designed by me.", corpus
    ).ok


def test_reemphasis_with_source_keywords_passes(corpus) -> None:
    assert check("PostgreSQL migration delivered with zero downtime.", corpus).ok


def test_written_number_matches_digits(corpus) -> None:
    """'four engineers' in the source supports '4 engineers' in the output."""
    assert check("Mentored 4 engineers through promotion.", corpus).ok


def test_plural_and_case_changes_pass(corpus) -> None:
    assert check("Mentored engineers; used Docker and Kubernetes.", corpus).ok


def test_empty_output_is_clean() -> None:
    assert check("", SourceCorpus.from_texts(RESUME_BACKEND)).ok


def test_projects_widen_the_corpus() -> None:
    """GitHub projects are source facts, so they license their own terms."""
    narrow = SourceCorpus.from_texts(RESUME_BACKEND)
    wide = SourceCorpus.from_texts(RESUME_BACKEND, "jobrunner — Playwright automation agent")

    assert not check("Built Playwright automation.", narrow).ok
    assert check("Built Playwright automation.", wide).ok


# --------------------------------------------------------------------------
# Gate 3 — 20 job descriptions × 3 résumés
# --------------------------------------------------------------------------


@pytest.mark.parametrize("resume_name", sorted(RESUMES))
@pytest.mark.parametrize("job_index", range(len(JOB_DESCRIPTIONS)))
async def test_gate3_every_combination_is_clean(resume_name: str, job_index: int) -> None:
    """Gate 3: every entity in every tailored output traces to its source."""
    resume = RESUMES[resume_name]
    job = JOB_DESCRIPTIONS[job_index]
    corpus = SourceCorpus.from_texts(resume)

    # The stub echoes source-derived text; a real provider swaps in here and
    # faces exactly the same gate.
    provider = StubProvider()
    result = await tailor_bullets(provider, bullets_of(resume), job, corpus)

    assert result.bullets, "every résumé must produce bullets"
    for rewrite in result.bullets:
        report = check(rewrite.tailored, corpus)
        assert report.ok, f"{resume_name}/{job_index}: {report.summary()}"


async def test_unknown_stub_response_is_rejected_not_used() -> None:
    """The stub's marker is not source text, so it must never reach output."""
    corpus = SourceCorpus.from_texts(RESUME_BACKEND)
    provider = StubProvider()

    rewrite = await tailor_bullet(provider, bullets_of(RESUME_BACKEND)[0], "job", corpus)

    assert rewrite.used_fallback
    assert rewrite.tailored == bullets_of(RESUME_BACKEND)[0]


async def test_fabricating_model_output_is_discarded() -> None:
    """The whole point: a model that invents does not get published."""
    corpus = SourceCorpus.from_texts(RESUME_BACKEND)
    original = "Mentored four engineers through promotion."
    provider = StubProvider({"Mentored": "Mentored 12 engineers at Google using Rust."})

    rewrite = await tailor_bullet(provider, original, "job", corpus)

    assert rewrite.tailored == original
    assert rewrite.used_fallback
    assert "unsupported" in (rewrite.rejected_reason or "")


async def test_clean_model_output_is_used() -> None:
    corpus = SourceCorpus.from_texts(RESUME_BACKEND)
    original = "Mentored four engineers through promotion."
    provider = StubProvider({"Mentored": "Mentored four engineers to promotion."})

    rewrite = await tailor_bullet(provider, original, "job", corpus)

    assert rewrite.tailored == "Mentored four engineers to promotion."
    assert rewrite.changed
    assert not rewrite.used_fallback


async def test_provider_failure_falls_back_to_source() -> None:
    class _Broken:
        name = "broken"

        async def complete(self, system, user, *, max_tokens=1024):
            raise RuntimeError("model is down")

        async def complete_json(self, system, user, schema):
            raise RuntimeError("model is down")

    corpus = SourceCorpus.from_texts(RESUME_BACKEND)
    original = "Mentored four engineers through promotion."

    rewrite = await tailor_bullet(_Broken(), original, "job", corpus)

    assert rewrite.tailored == original
    assert "provider error" in (rewrite.rejected_reason or "")


def test_padding_is_rejected() -> None:
    """A rewrite far longer than its source is where invented detail hides."""
    corpus = SourceCorpus.from_texts(RESUME_BACKEND)
    original = "Mentored four engineers."
    padded = "Mentored four engineers " + "through promotion and review cycles " * 6

    accepted, reason, _ = vet(original, padded, corpus)

    assert not accepted
    assert "longer" in (reason or "")


# --------------------------------------------------------------------------
# Diff
# --------------------------------------------------------------------------


async def test_diff_shows_only_changed_lines() -> None:
    corpus = SourceCorpus.from_texts(RESUME_BACKEND)
    provider = StubProvider({"Mentored": "Mentored four engineers to promotion."})

    result = await tailor_bullets(provider, bullets_of(RESUME_BACKEND), "job", corpus)
    summary = summarize(result)

    assert summary.changed == 1
    assert summary.unchanged == len(result.bullets) - 1
    assert summary.rejected >= 1


def test_inline_diff_marks_both_sides() -> None:
    from packages.tailor.diff import inline_html

    markup = inline_html("Mentored four engineers", "Mentored four engineers to promotion")
    assert "<ins>" in markup
    assert "Mentored" in markup


def test_diff_escapes_source_text() -> None:
    from packages.tailor.diff import inline_html

    assert "<script>" not in inline_html("a", "<script>alert(1)</script>")


def test_unified_diff_is_readable() -> None:
    text = unified(["one", "two"], ["one", "three"])
    assert "-two" in text
    assert "+three" in text


def test_empty_diff_renders_nothing() -> None:
    from packages.tailor.rewrite import TailorResult

    assert render_html(summarize(TailorResult())) == ""


# --------------------------------------------------------------------------
# Gate 3 — the tailored PDF must survive a parser round-trip
# --------------------------------------------------------------------------


async def test_tailored_pdf_round_trips_through_the_parser() -> None:
    """Gate 3: render the tailored résumé, then read it back.

    An ATS reads the PDF, not the HTML. A layout that renders beautifully and
    extracts to nothing is worse than a plain one, so the check is that the
    facts survive the round trip.
    """
    from packages.tailor.assemble import assemble_pdf
    from packages.tailor.parse import extract_text, parse_text

    corpus = SourceCorpus.from_texts(RESUME_DATA)
    provider = StubProvider({"Owned": "Owned the Airflow deployment serving 30 analysts."})
    result = await tailor_bullets(provider, bullets_of(RESUME_DATA), JOB_DESCRIPTIONS[2], corpus)

    source = parse_text(RESUME_DATA)
    source.sections["experience"] = result.tailored_lines

    pdf = assemble_pdf(source, [])
    recovered = extract_text(pdf, "tailored.pdf")

    # Every claim-bearing entity that went in comes back out.
    for line in result.tailored_lines:
        for entity in extract_entities(line):
            assert entity.text in recovered, f"{entity.text!r} did not survive the PDF"

    # And the recovered document still parses into sections.
    reparsed = parse_text(recovered)
    assert "experience" in reparsed.sections


async def test_round_tripped_pdf_still_passes_the_guard() -> None:
    """Rendering must not introduce a claim the source never made."""
    from packages.tailor.assemble import assemble_pdf
    from packages.tailor.parse import extract_text, parse_text

    corpus = SourceCorpus.from_texts(RESUME_FRONTEND)
    source = parse_text(RESUME_FRONTEND)

    recovered = extract_text(assemble_pdf(source, []), "out.pdf")

    report = check(recovered, corpus)
    assert report.ok, report.summary()


# --------------------------------------------------------------------------
# Attribution: a fact is not grounded just because it is true somewhere
# --------------------------------------------------------------------------

RESUME_TWO_EMPLOYERS = """Jane Doe
jane@example.com

Experience
Acme Corp - Backend Engineer
- Maintained the billing service and reduced invoice errors.
Globex Inc - Data Engineer
- Built a streaming pipeline processing 40TB per day across 12 regions.

Skills
Python, PostgreSQL, Kubernetes
"""


@pytest.fixture
def two_employers() -> SourceCorpus:
    return SourceCorpus.from_resume(parse_text(RESUME_TWO_EMPLOYERS))


ACME_BULLET = "- Maintained the billing service and reduced invoice errors."


def test_resume_splits_into_one_item_per_employer(two_employers: SourceCorpus) -> None:
    assert [item.ref for item in two_employers.items] == ["experience:0", "experience:1"]


def test_metric_from_another_employer_is_rejected(two_employers: SourceCorpus) -> None:
    """The case a document-wide corpus cannot see.

    Every number here appears in the résumé, so the old whole-document check
    accepted it. It is still a fabrication: the throughput was earned at
    Globex and this sentence attributes it to Acme.
    """
    drifted = (
        "- Maintained the billing service, reduced invoice errors across 12 "
        "regions processing 40TB per day."
    )
    accepted, reason, report = vet(ACME_BULLET, drifted, two_employers)

    assert not accepted
    assert report.scope_ref == "experience:0"
    unsupported = {v.entity.text for v in report.violations}
    assert {"12", "40TB"} <= unsupported
    assert reason is not None


def test_honest_rewrite_of_the_same_entry_still_passes(two_employers: SourceCorpus) -> None:
    """Scoping must not cost legitimate rewrites."""
    accepted, reason, _ = vet(
        ACME_BULLET, "- Owned the billing service, cutting invoice errors.", two_employers
    )
    assert accepted, reason


def test_shared_sections_are_available_to_every_entry(two_employers: SourceCorpus) -> None:
    """Skills describe the person, not one job, so a bullet may reach them."""
    accepted, reason, _ = vet(
        ACME_BULLET,
        "- Maintained the billing service in Python, reducing invoice errors.",
        two_employers,
    )
    assert accepted, reason


def test_flat_corpus_degrades_to_document_scope_and_says_so() -> None:
    """No structure means no attribution claim — recorded, not pretended."""
    corpus = SourceCorpus.from_texts(RESUME_TWO_EMPLOYERS)

    assert corpus.items == ()
    assert corpus.locate(ACME_BULLET) is None

    _, _, report = vet(ACME_BULLET, "- Maintained the billing service.", corpus)
    assert report.scope_ref is None
