"""Only a bullet that can truthfully gain a term is revised, and the PDF is read back.

Rewriting every bullet of the owner's résumé for one posting gained no
keyword, lowered the recruiter score and lost two numbers and three model
names (2026-10-13). `packages/tailor/targeted.py` sorts the posting's terms
against the résumé first, sends a model only the bullets that hold evidence
for a term the résumé does not yet state, and keeps a revision only if it
adds the term and takes nothing away. `packages/tailor/verify.py` then reads
the finished PDF's text, because that is what an employer's parser reads.

Everything here is offline: a synthetic résumé, a synthetic posting and
`StubProvider`. Three bullets carry it:

    Built a classification pipeline over 148 years of fire data, reaching ROC-AUC 0.72.
    Loaded 1,000 billing records with pytest checks.
    Wrote the on-call handbook for the support rotation.

and the posting requires "classification", "automated testing" and
"Kubernetes". The first is on the résumé. The second is not, and the pytest
bullet is evidence of it (`practices.py`, where that is argued). The third is
a gap and stays one.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from packages.core.models import Application, Candidate, Match, Profile, Resume, User
from packages.core.storage import get_storage
from packages.llm.prompts import TAILOR_TARGETED_SYSTEM, identify
from packages.llm.provider import StubProvider
from packages.tailor import figures, verify
from packages.tailor.assemble import assemble_pdf
from packages.tailor.guard import SourceCorpus
from packages.tailor.parse import ParsedResume, extract_text
from packages.tailor.practices import evidence_in, practice_for
from packages.tailor.publish import apply_rewrites, publish_tailored
from packages.tailor.rewrite import tailor_bullets, vet
from packages.tailor.targeted import (
    NO_GAIN,
    Status,
    Target,
    classify,
    judge,
    posting_terms,
    revise,
    taken_away,
)
from tests.test_batch_tailor import _match

CLASSIFIER = "Built a classification pipeline over 148 years of fire data, reaching ROC-AUC 0.72."
LOADER = "Loaded 1,000 billing records with pytest checks."
HANDBOOK = "Wrote the on-call handbook for the support rotation."
SKILLS = "Languages & Tools  Python, pytest, pandas"

POSTING = "Requirements:\nClassification, automated testing, and Kubernetes.\n"

#: Adds the term beside its evidence and takes nothing away.
ACCEPTED = "Loaded 1,000 billing records with automated testing via pytest checks."
DROPS_THE_COUNT = "Loaded billing records with automated testing via pytest checks."
DROPS_THE_TOOL = "Loaded 1,000 billing records with automated testing checks."
ADDS_A_GAP = "Loaded 1,000 billing records with automated testing via pytest checks on Kubernetes."


def resume(experience: list[str], **sections: list[str]) -> ParsedResume:
    body = {"experience": experience, **sections}
    raw = [line for name, lines in body.items() for line in (name.title(), *lines)]
    return ParsedResume.model_validate(
        {
            "contact": {"name": "Fixture Owner", "email": "fixture@example.com", "links": []},
            "preamble": [],
            "sections": body,
            "raw_lines": raw,
        }
    )


RESUME = resume([CLASSIFIER, LOADER, HANDBOOK], skills=[SKILLS])


class Scripted(StubProvider):
    """Answers in the order given, repeating the last. No network, like its parent."""

    def __init__(self, *answers: str) -> None:
        super().__init__()
        self._answers = list(answers)

    async def complete(self, system, user, *, max_tokens=1024, temperature=0.2) -> str:  # noqa: ANN001
        self.calls.append((system, user))
        return self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]


def by_term(plan) -> dict[str, object]:  # noqa: ANN001
    return {term.term.lower(): term for term in plan.terms}


# --------------------------------------------------------------------------
# The posting's terms, sorted against the résumé
# --------------------------------------------------------------------------


def test_a_practice_is_one_term_of_the_posting() -> None:
    terms = [term.lower() for term in posting_terms(POSTING)]

    assert "automated testing" in terms
    assert "automated" not in terms and "testing" not in terms


def test_the_three_terms_are_sorted_three_ways() -> None:
    plan = classify(RESUME, POSTING)
    terms = by_term(plan)

    assert plan.bullets == (CLASSIFIER, LOADER, HANDBOOK)
    assert terms["classification"].status is Status.COVERED
    assert terms["kubernetes"].status is Status.GAP
    testing = terms["automated testing"]
    assert testing.status is Status.SUPPORTED
    assert testing.via == "pytest"


def test_the_supporting_entries_are_recorded() -> None:
    """The pytest bullet and the skills line both name the tool. The bullet
    is the one that can be revised."""
    terms = by_term(classify(RESUME, POSTING))

    assert terms["classification"].sources == ("experience:0",)
    assert terms["automated testing"].sources == ("experience:1", "shared")
    assert terms["automated testing"].bullet == 1
    assert terms["kubernetes"].sources == () and terms["kubernetes"].bullet is None


def test_only_the_bullet_with_the_evidence_is_a_target() -> None:
    plan = classify(RESUME, POSTING)

    assert plan.targets == {
        1: (Target(term="automated testing", evidence="pytest", practice=True),)
    }
    assert plan.gaps == ("Kubernetes",)
    assert plan.calls_needed == 2


# --------------------------------------------------------------------------
# What is sent, and what comes back
# --------------------------------------------------------------------------


async def test_only_the_pytest_bullet_is_sent() -> None:
    provider = Scripted(ACCEPTED)

    revision = await revise(provider, RESUME, POSTING)

    assert revision.calls == 1 and len(provider.calls) == 1
    system, user = provider.calls[0]
    assert identify(system) is TAILOR_TARGETED_SYSTEM
    assert LOADER in user
    assert "automated testing (evidence: pytest)" in user
    # Not the other bullets, and not the posting: nothing to reach for.
    assert CLASSIFIER not in user and HANDBOOK not in user
    assert "Kubernetes" not in user and "Requirements" not in user


async def test_an_accepted_revision_adds_the_term_and_keeps_the_facts() -> None:
    revision = await revise(Scripted(ACCEPTED), RESUME, POSTING)

    first, second, third = revision.result.bullets
    assert second.changed and second.tailored == ACCEPTED
    assert "automated testing" in second.tailored
    assert "1,000" in second.tailored and "pytest" in second.tailored
    assert revision.gained == ("automated testing",)
    assert revision.message == "1 of 1 supported terms added, in 1 model calls"
    # The others were never sent and are the résumé's own.
    assert (first.tailored, first.changed) == (CLASSIFIER, False)
    assert (third.tailored, third.changed) == (HANDBOOK, False)
    assert revision.result.rejected == 0 and revision.result.provider_failures == 0


@pytest.mark.parametrize(
    "answer, why",
    [
        (DROPS_THE_COUNT, "'1,000'"),
        (DROPS_THE_TOOL, "'pytest'"),
        (ADDS_A_GAP, "'Kubernetes'"),
    ],
)
async def test_a_bad_revision_is_refused_and_the_bullet_is_the_resumes(
    answer: str, why: str
) -> None:
    provider = Scripted(answer)

    revision = await revise(provider, RESUME, POSTING)

    loader = revision.result.bullets[1]
    assert loader.tailored == LOADER and not loader.changed
    assert loader.guard_refused and not loader.provider_failed
    assert loader.rejected_reason is not None and why in loader.rejected_reason
    assert revision.gained == ()
    # Asked again once with the reason, and no more.
    assert len(provider.calls) == 2
    assert "could not be used" in provider.calls[1][1]
    assert revision.outcomes[0].attempts == 2 and len(revision.outcomes[0].refusals) == 2


async def test_the_dropped_count_is_named_on_the_result() -> None:
    revision = await revise(Scripted(DROPS_THE_COUNT), RESUME, POSTING)

    assert revision.result.bullets[1].dropped_figures == ["1,000"]


async def test_the_one_repair_can_succeed() -> None:
    provider = Scripted(DROPS_THE_COUNT, ACCEPTED)

    revision = await revise(provider, RESUME, POSTING)

    assert revision.result.bullets[1].tailored == ACCEPTED
    assert revision.calls == 2
    assert revision.outcomes[0].refusals and "'1,000'" in revision.outcomes[0].refusals[0]


async def test_a_model_that_returns_the_bullet_unchanged_adds_nothing() -> None:
    revision = await revise(Scripted(LOADER), RESUME, POSTING)

    loader = revision.result.bullets[1]
    assert loader.tailored == LOADER and not loader.changed
    assert loader.rejected_reason == "adds none of the terms it was revised for"


async def test_a_provider_that_fails_keeps_the_bullet_and_is_not_a_refusal() -> None:
    class Down(StubProvider):
        async def complete(self, system, user, *, max_tokens=1024, temperature=0.2) -> str:  # noqa: ANN001
            self.calls.append((system, user))
            raise TimeoutError

    provider = Down()
    revision = await revise(provider, RESUME, POSTING)

    loader = revision.result.bullets[1]
    assert loader.tailored == LOADER and loader.provider_failed and not loader.guard_refused
    assert revision.result.provider_failures == 1 and len(provider.calls) == 2


# --------------------------------------------------------------------------
# Nothing to gain, nothing asked
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "posting",
    [
        "Requirements:\nClassification and pytest.\n",  # all covered
        "Requirements:\nClassification and Kubernetes.\n",  # covered, and a gap
    ],
)
async def test_no_opportunity_makes_no_model_call(posting: str) -> None:
    provider = Scripted(ACCEPTED)

    revision = await revise(provider, RESUME, posting)

    assert provider.calls == [] and revision.calls == 0
    assert revision.message == NO_GAIN
    assert [bullet.tailored for bullet in revision.result.bullets] == [CLASSIFIER, LOADER, HANDBOOK]
    assert revision.result.changed_count == 0


# --------------------------------------------------------------------------
# A term goes where its evidence is
# --------------------------------------------------------------------------

EXPORTS = "Built billing exports in Python for the finance team."
RANKER = "Shipped an ML ranking service for search."
LEDGER = "Verified 1,000 ledger rows with pytest checks."


async def test_another_projects_tool_does_not_become_an_employers_claim() -> None:
    """pytest is in a side project. The bullets being tailored are the
    employer's, so the term is recorded with its source and no bullet is
    revised for it."""
    parsed = resume(
        [EXPORTS, HANDBOOK],
        projects=["Ledger Checker — Side Project   [GitHub]", "Python, pytest", LEDGER],
    )
    provider = Scripted("Built billing exports in Python with automated testing.")

    plan = classify(parsed, POSTING)
    testing = by_term(plan)["automated testing"]
    revision = await revise(provider, parsed, POSTING)

    assert testing.status is Status.SUPPORTED and testing.via == "pytest"
    assert testing.sources and all(ref.startswith("projects:") for ref in testing.sources)
    assert testing.bullet is None and plan.targets == {}
    assert provider.calls == [] and revision.message == NO_GAIN


async def test_a_bullet_revised_for_one_term_cannot_pick_up_anothers() -> None:
    """The employer's bullet is revised to write `ML` out. The model also
    adds the practice a side project is evidence of. That term is off limits
    to this bullet, whatever else the revision got right."""
    parsed = resume(
        [RANKER, HANDBOOK],
        projects=["Ledger Checker — Side Project   [GitHub]", "Python, pytest", LEDGER],
    )
    posting = "Requirements:\nMachine learning and automated testing.\n"
    greedy = "Shipped a machine learning (ML) ranking service for search with automated testing."

    plan = classify(parsed, posting)
    revision = await revise(Scripted(greedy), parsed, posting)

    assert set(plan.targets) == {0}
    assert [target.term for target in plan.targets[0]] == ["machine learning"]
    ranker = revision.result.bullets[0]
    assert ranker.tailored == RANKER and ranker.guard_refused
    assert ranker.rejected_reason is not None and "'automated testing'" in ranker.rejected_reason


async def test_a_term_the_resume_states_elsewhere_cannot_be_moved_onto_the_bullet() -> None:
    """ "classification" is covered, by the first bullet. It is not the
    loader's to claim."""
    moved = (
        "Loaded 1,000 billing records with automated testing via pytest checks for classification."
    )

    revision = await revise(Scripted(moved), RESUME, POSTING)

    loader = revision.result.bullets[1]
    assert loader.tailored == LOADER and loader.guard_refused
    # The fabrication guard gets there first: the word is another entry's.
    assert loader.rejected_reason is not None
    assert "classification" in loader.rejected_reason.lower()


async def test_the_tool_has_to_stay_even_where_no_skills_line_lists_it() -> None:
    """With no skills line the technology guard has no list to hold pytest
    to. A targeted revision takes no word out of the bullet, and that does."""
    parsed = resume([CLASSIFIER, LOADER, HANDBOOK])

    revision = await revise(Scripted(DROPS_THE_TOOL), parsed, POSTING)

    loader = revision.result.bullets[1]
    assert loader.tailored == LOADER
    assert loader.rejected_reason is not None and "takes 'pytest' out" in loader.rejected_reason


async def test_the_ordinary_rewrite_is_refused_the_term_as_before() -> None:
    """It does not read the table. To it the term is one the résumé does not
    state, and a rewrite that uses it took it from the posting."""
    corpus = SourceCorpus.from_resume(RESUME)

    result = await tailor_bullets(StubProvider({"Loaded": ACCEPTED}), [LOADER], POSTING, corpus)

    assert result.tailored_lines == [LOADER] and result.rejected == 1
    reason = result.bullets[0].rejected_reason
    assert reason is not None and "from the posting" in reason


def test_the_table_goes_one_way() -> None:
    assert practice_for("automated testing") is practice_for("Test Automation")
    assert evidence_in("automated testing", LOADER) == "pytest"
    assert evidence_in("automated testing", CLASSIFIER) is None
    # A practice is evidence of no tool, and a tool of no other practice.
    assert practice_for("pytest") is None
    assert practice_for("Kubernetes") is None and practice_for("continuous integration") is None


# --------------------------------------------------------------------------
# A term the résumé says another way
# --------------------------------------------------------------------------


async def test_a_short_form_is_written_out_in_the_bullet_that_has_it() -> None:
    parsed = resume([RANKER, HANDBOOK])
    posting = "Requirements:\nMachine learning.\n"
    written_out = "Shipped a machine learning (ML) ranking service for search."

    plan = classify(parsed, posting)
    term = by_term(plan)["machine learning"]
    revision = await revise(Scripted(written_out), parsed, posting)

    assert term.status is Status.SYNONYM and term.via == "ml" and term.bullet == 0
    assert plan.targets == {0: (Target(term="machine learning", evidence="ML"),)}
    assert revision.result.bullets[0].tailored == written_out
    assert revision.gained == ("machine learning",)


def test_writing_a_short_form_out_takes_nothing_away() -> None:
    assert taken_away(RANKER, "Shipped a machine learning ranking service for search.") == ()


# --------------------------------------------------------------------------
# A targeted revision adds, and takes nothing away
# --------------------------------------------------------------------------

TUNED = "Fine-tuned DeBERTa-NLI in 2023 on 148 hours of calls with pytest checks."
TUNED_WITH_THE_TERM = (
    "Fine-tuned DeBERTa-NLI in 2023 on 148 hours of calls with automated testing in pytest checks."
)


@pytest.mark.parametrize(
    "change, named",
    [
        (("DeBERTa-NLI", "a model"), "'DeBERTa-NLI'"),
        ((" in 2023", ""), "'2023'"),
        ((" of calls", ""), "'calls'"),
        # A unit that is not the bullet's is refused one check earlier, as a
        # noun the entry does not have.
        (("148 hours", "148 minutes"), "'minutes'"),
    ],
)
def test_a_model_name_a_date_a_noun_and_a_unit_all_have_to_stay(
    change: tuple[str, str], named: str
) -> None:
    parsed = resume([TUNED, HANDBOOK])
    target = (Target(term="automated testing", evidence="pytest", practice=True),)
    candidate = TUNED_WITH_THE_TERM.replace(*change)
    assert candidate != TUNED_WITH_THE_TERM

    added, reason, _, _ = judge(TUNED, candidate, target, SourceCorpus.from_resume(parsed), ())

    assert added == ()
    assert reason is not None and named in reason


def test_the_same_bullet_with_only_the_term_added_is_kept() -> None:
    parsed = resume([TUNED, HANDBOOK])
    target = (Target(term="automated testing", evidence="pytest", practice=True),)
    corpus = SourceCorpus.from_resume(parsed)

    added, reason, _, _ = judge(TUNED, TUNED_WITH_THE_TERM, target, corpus, ())

    assert added == ("automated testing",), reason


# --------------------------------------------------------------------------
# 0.72
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "candidate",
    [
        "Built a classification pipeline over 148 years of fire data.",
        "Built a classification pipeline over 148 years of fire data, reaching ROC-AUC 0.7.",
        "Built a classification pipeline over 148 years of fire data, reaching ROC-AUC 0.75.",
        "Built a classification pipeline over 148 years of fire data, reaching ROC-AUC 72.",
    ],
)
def test_the_score_may_not_be_lost_or_altered(candidate: str) -> None:
    accepted, reason, _ = vet(CLASSIFIER, candidate, SourceCorpus.from_resume(RESUME))

    assert not accepted, candidate
    assert figures.dropped(CLASSIFIER, candidate) == ("0.72",)
    assert reason is not None


def test_the_score_written_the_same_way_is_kept() -> None:
    candidate = (
        "Built a classification pipeline over 148 years of fire data, reaching a ROC-AUC of 0.72."
    )

    accepted, reason, report = vet(CLASSIFIER, candidate, SourceCorpus.from_resume(RESUME))

    assert accepted, reason
    assert report.dropped_figures == ()


# --------------------------------------------------------------------------
# The PDF, read back
# --------------------------------------------------------------------------


async def _owner(session) -> tuple[Candidate, Profile]:  # noqa: ANN001
    suffix = uuid.uuid4().hex[:8]
    user = User(email=f"t-{suffix}@example.com")
    session.add(user)
    await session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email=f"t-{suffix}@example.com")
    session.add(candidate)
    await session.flush()
    base = Resume(
        candidate_id=candidate.id,
        version=1,
        storage_ref=f"r/{suffix}",
        parsed_json=RESUME.model_dump(mode="json"),
    )
    session.add(base)
    await session.flush()
    profile = Profile(candidate_id=candidate.id, label="default", base_resume_id=base.id)
    session.add(profile)
    await session.flush()
    return candidate, profile


async def test_the_pdf_holds_the_term_and_the_facts_and_nothing_is_attached(db_session) -> None:
    candidate, profile = await _owner(db_session)
    match = await _match(db_session, profile, decision="interested")
    application = Application(
        candidate_id=candidate.id,
        profile_id=profile.id,
        url="https://x.test/targeted",
        ats="greenhouse",
    )
    db_session.add(application)
    await db_session.flush()

    revision = await revise(Scripted(ACCEPTED), RESUME, POSTING)
    published = await publish_tailored(
        db_session, candidate_id=candidate.id, parsed=RESUME, result=revision.result
    )
    assert published is not None
    pdf = get_storage().get(published.storage_ref)
    tailored = apply_rewrites(RESUME, revision.result)

    check = verify.check_pdf(pdf, tailored, terms=revision.gained)
    text = " ".join(extract_text(pdf, "resume.pdf").split())

    assert check.ok, check
    assert check.missing_terms == () and check.missing_figures == ()
    assert check.missing_technologies == ()
    assert check.extracted_chars > 0 and not check.garbled and check.pages == 1
    for kept in ("automated testing", "1,000", "pytest", "148", "0.72", "classification"):
        assert kept in text, kept
    assert "Kubernetes" not in text

    # Published is all that happened. Nothing points at it, and it is not a
    # cached tailoring that a later run would be served.
    await db_session.refresh(match)
    await db_session.refresh(application)
    assert match.tailored_resume_id is None and application.tailored_resume_id is None
    assert published.tailored_key is None and published.is_default is False
    matches = await db_session.scalar(
        select(func.count()).select_from(Match).where(Match.profile_id == profile.id)
    )
    assert matches == 1
    assert (await db_session.get(Profile, profile.id)).base_resume_id != published.id


@pytest.mark.parametrize("answer", [DROPS_THE_COUNT, DROPS_THE_TOOL, ADDS_A_GAP])
async def test_refused_text_never_reaches_the_pdf(db_session, answer: str) -> None:
    candidate, _ = await _owner(db_session)

    revision = await revise(Scripted(answer), RESUME, POSTING)
    published = await publish_tailored(
        db_session, candidate_id=candidate.id, parsed=RESUME, result=revision.result
    )
    assert published is not None
    text = " ".join(extract_text(get_storage().get(published.storage_ref), "resume.pdf").split())

    assert LOADER in text
    assert "automated testing" not in text and "Kubernetes" not in text
    stored = ParsedResume.model_validate(published.parsed_json)
    assert stored.section("experience") == [CLASSIFIER, LOADER, HANDBOOK]


async def test_the_check_says_when_a_term_is_not_in_the_pdf() -> None:
    revision = await revise(Scripted(ACCEPTED), RESUME, POSTING)
    tailored = apply_rewrites(RESUME, revision.result)
    pdf = assemble_pdf(tailored)

    check = verify.check_pdf(pdf, tailored, terms=("automated testing", "Kubernetes"))

    assert check.missing_terms == ("Kubernetes",)
    assert not check.ok


async def test_the_check_says_when_a_fact_is_not_in_the_pdf() -> None:
    """The PDF of the base résumé, held to a résumé with a bullet it lacks."""
    pdf = assemble_pdf(RESUME)
    other = resume(
        [CLASSIFIER, "Loaded 2,500 billing records with pandas checks.", HANDBOOK], skills=[SKILLS]
    )

    check = verify.check_pdf(pdf, other)

    assert check.missing_figures == ("2,500",)
    assert not check.ok


# --------------------------------------------------------------------------
# Before and after, like for like
# --------------------------------------------------------------------------


async def test_the_gain_is_reported_document_to_document_and_pdf_to_pdf() -> None:
    revision = await revise(Scripted(ACCEPTED), RESUME, POSTING)
    tailored = apply_rewrites(RESUME, revision.result)

    documents = verify.compare(RESUME, tailored, POSTING, representation="document")
    pdfs = verify.compare_pdfs(assemble_pdf(RESUME), assemble_pdf(tailored), POSTING)

    for comparison in (documents, pdfs):
        assert comparison.after.stated == comparison.before.stated + 1, comparison.representation
        assert comparison.after.stated_of == comparison.before.stated_of == 3
        assert comparison.after.backed > comparison.before.backed
        # Flagged, and by arithmetic. The scan score is the share of matched
        # terms that sit in the top of the page, and the term gained is in the
        # second bullet: one of one became one of three. Nothing at the top
        # changed. The owner is shown it, and nothing is decided by it.
        assert comparison.regressions, comparison.representation
        assert all(fall.startswith(("scan ", "recruiter ")) for fall in comparison.regressions)
    assert pdfs.representation == "pdf"


async def test_a_gain_at_the_top_of_the_page_flags_nothing() -> None:
    """The same three bullets with the pytest one first."""
    parsed = resume([LOADER, CLASSIFIER, HANDBOOK], skills=[SKILLS])
    revision = await revise(Scripted(ACCEPTED), parsed, POSTING)
    tailored = apply_rewrites(parsed, revision.result)

    documents = verify.compare(parsed, tailored, POSTING, representation="document")
    pdfs = verify.compare_pdfs(assemble_pdf(parsed), assemble_pdf(tailored), POSTING)

    assert revision.gained == ("automated testing",)
    assert documents.regressions == () and pdfs.regressions == ()
    assert documents.after.stated == documents.before.stated + 1


def test_a_fall_in_any_part_is_listed() -> None:
    base = verify.read(RESUME, POSTING)
    worse = verify.Reading(**{**base.__dict__, "technical": base.technical - 0.3, "stated": 0})

    comparison = verify.Comparison("document", before=base, after=worse)

    assert len(comparison.regressions) == 2
    assert comparison.regressions[0].startswith("technical ")
    assert comparison.regressions[1] == f"stated terms {base.stated} to 0"


async def test_new_layout_and_parse_findings_are_the_revisions_own() -> None:
    before = verify.check_pdf(assemble_pdf(RESUME), RESUME)
    same = verify.check_pdf(assemble_pdf(RESUME), RESUME)

    assert verify.new_findings(before, same) == ()
    grown = verify.PdfCheck(
        **{
            **same.__dict__,
            "pages": before.pages + 1,
            "layout_findings": (*same.layout_findings, "thin_last_page"),
        }
    )
    assert verify.new_findings(before, grown) == (
        "layout: thin_last_page",
        f"layout: {before.pages} pages to {before.pages + 1}",
    )


# --------------------------------------------------------------------------
# What the command prints before it asks anything
# --------------------------------------------------------------------------


def test_the_plan_is_described_without_a_model() -> None:
    from scripts.tailor_targeted import describe_plan

    lines = describe_plan(classify(RESUME, POSTING))
    text = "\n".join(lines)

    assert "automated testing: by 'pytest' in experience:1, shared; bullet 2 can be revised" in text
    assert "gaps, never written in: Kubernetes" in text
    assert "1 of 3 bullets would be sent, 2 model calls at most:" in text
    assert "bullet 2: automated testing (evidence: pytest)" in text
    assert NO_GAIN not in text


def test_a_plan_with_nothing_to_gain_says_so() -> None:
    from scripts.tailor_targeted import describe_plan

    lines = describe_plan(classify(RESUME, "Requirements:\nClassification and Kubernetes.\n"))

    assert lines[-1] == NO_GAIN
    assert not any("would be sent" in line for line in lines)
