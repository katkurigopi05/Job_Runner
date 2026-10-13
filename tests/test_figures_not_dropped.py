"""A rewrite may not quietly delete or alter a number the bullet states.

Found on 2026-10-13 by tailoring the owner's résumé for one posting and
reading the eight rewrites that were accepted. Two of them had lost figures:
a model's test score and the span of its training data from one bullet, a
count from another. Nothing was invented, so nothing was refused. The ATS
keyword score did not move and the recruiter score fell.

`tests/test_technology_not_dropped.py` is the same defect for a library name
and was closed the same way: a check that reads what is *missing*. A number
is the other thing a bullet has that a reader looks for, and the fabrication
guard already protects it in one direction only. It refuses a number the
source does not have. It did not refuse a source's number going away.

What counts as a figure is `packages/tailor/figures.py`'s to say, and three
things are decided there on purpose:

- **A value, not a spelling.** `1,000`, `1000` and `1k` are one figure.
  `30%` and `30` are two.
- **A year is not a figure**, and neither is a phone number, an address or a
  page number. The guard already reads a bare 19xx or 20xx as a year; this
  reads it the same way, unless the text marks it as a count (`2,000`,
  `2000+`, `over 2000`).
- **A digit inside a name is the name's.** `EC2`, `S3` and `GPT-4` are held
  by the checks that read names.

Every existing refusal keeps its reason: this check runs last, so it only
refuses what was accepted before.
"""

from __future__ import annotations

import uuid

import pytest

from apps.worker import apply_job
from packages.core.models import Application, Candidate, Profile, Resume, User
from packages.core.storage import get_storage
from packages.llm.provider import StubProvider
from packages.tailor import figures
from packages.tailor.bullets import tailorable_bullets
from packages.tailor.cache import find_cached
from packages.tailor.guard import SourceCorpus
from packages.tailor.parse import ParsedResume, parse_resume, parse_text
from packages.tailor.publish import apply_rewrites, publish_tailored
from packages.tailor.rewrite import tailor_bullet, tailor_bullets, vet

#: Invented, like the rest of this résumé. The shape is the one that was lost:
#: a span of data and a test score in one bullet.
SCORE = (
    "Trained a churn classifier on 148 months of billing history, reaching a test ROC-AUC of 0.72."
)
RECORDS = (
    "Provisioned warehouses and loaded 1,000 billing records with pytest checks on every load."
)
ERRORS = "Maintained the billing service and reduced invoice errors by 30%."
BUILDS = "Cut build times from 10-15 minutes to 2-3 minutes across 12 million events a day."

RESUME = "\n".join(
    [
        "Projects",
        "Churn Model — Capstone   [GitHub]",
        "Python, scikit-learn, pandas, pytest",
        SCORE,
        RECORDS,
        ERRORS,
        BUILDS,
        "Skills",
        "Data & Tools  Python, scikit-learn, pandas, pytest, Docker",
        "",
    ]
)

POSTING = "We need an engineer with Python and data pipeline experience for our billing team."


@pytest.fixture
def corpus() -> SourceCorpus:
    return SourceCorpus.from_resume(parse_text(RESUME))


def values(text: str) -> list[str]:
    return [figure.text for figure in figures.read(text)]


# --------------------------------------------------------------------------
# What a figure is
# --------------------------------------------------------------------------


def test_a_bullets_numbers_are_read() -> None:
    assert values(SCORE) == ["148", "0.72"]
    assert values(RECORDS) == ["1,000"]
    assert values(ERRORS) == ["30%"]
    assert values(BUILDS) == ["10", "15", "2", "3", "12 million"]


@pytest.mark.parametrize(
    "one, other",
    [
        ("loaded 1,000 records", "loaded 1000 records"),
        ("loaded 1,000 records", "loaded 1k records"),
        ("by 30%", "by 30 %"),
        ("by 30%", "by 30 percent"),
        ("12 million events", "12M events"),
        ("12 million events", "12,000,000 events"),
        ("a score of 0.72", "a score of 0.720"),
        ("a score of 0.72", "a score of .72"),
        ("in 800ms", "in 800 ms"),
        ("a $1.2M budget", "a $1,200,000 budget"),
        ("from 10-15 minutes", "from 10 to 15 minutes"),
        ("from 10–15 minutes", "from 10-15 minutes"),
        ("by 5-10%", "by 5% to 10%"),
        ("across 3 services", "across three services"),
        ("over 1,000 users", "1,000+ users"),
    ],
)
def test_two_ways_of_writing_one_value_are_the_same_figure(one: str, other: str) -> None:
    assert figures.dropped(one, other) == ()
    assert figures.dropped(other, one) == ()


@pytest.mark.parametrize(
    "one, other",
    [
        ("by 30%", "by 30"),
        ("12 million events", "12 events"),
        ("12 million events", "12 thousand events"),
        ("a score of 0.72", "a score of 0.7"),
        ("loaded 1,000 records", "loaded 100 records"),
    ],
)
def test_a_different_value_is_a_different_figure(one: str, other: str) -> None:
    assert figures.dropped(one, other) != ()


@pytest.mark.parametrize(
    "text",
    [
        "Led the migration from Jan 2023 to March 2024.",
        "Shipped the first release in 2022.",
        "Research assistant (2021–2023), then engineer since 2023.",
        "Graduated 05/2023 and joined in Q3 2023.",
        "Interned in summer 2021 and again on June 3, 2022.",
        "Released 2023-05-17.",
        "Call (415) 555-0134 or +1 415-555-0134.",
        "Write to jane99@example.com or see github.com/jane99/repo2 and https://example.com/v2/docs.",
        "Page 2 of 3",
        "p. 12",
    ],
)
def test_dates_and_contact_details_are_not_figures(text: str) -> None:
    assert values(text) == []


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Served 2,000 users", ["2,000"]),
        ("Served 2000+ users", ["2000"]),
        ("Served over 2000 users", ["2000"]),
        ("Served more than 2000 users", ["2000"]),
        ("Handled 2000 requests per second", ["2000"]),
    ],
)
def test_a_count_that_looks_like_a_year_is_a_figure_when_the_text_says_so(
    text: str, expected: list[str]
) -> None:
    assert values(text) == expected


def test_a_digit_inside_a_name_is_the_names() -> None:
    assert (
        values("Deployed on EC2 and S3 with k8s, OAuth2, GPT-4, Python3 and an H-1B, COVID-19")
        == []
    )


def test_a_count_joined_to_its_noun_is_a_figure() -> None:
    assert values("a 4-model team capstone with a 12-month forecast") == ["4", "12"]


# --------------------------------------------------------------------------
# The numbers the owner's rewrites lost
# --------------------------------------------------------------------------


def test_a_dropped_score_is_refused_and_named(corpus) -> None:
    rewrite = "Trained a churn classifier on 148 months of billing history."

    accepted, reason, report = vet(SCORE, rewrite, corpus)

    assert not accepted
    assert reason is not None and "'0.72'" in reason
    assert report.dropped_figures == ("0.72",)


def test_a_dropped_span_is_refused_with_the_score_kept(corpus) -> None:
    rewrite = "Trained a churn classifier on billing history, reaching a test ROC-AUC of 0.72."

    accepted, reason, report = vet(SCORE, rewrite, corpus)

    assert not accepted
    assert report.dropped_figures == ("148",)
    assert reason is not None and "0.72" not in reason


def test_a_dropped_count_with_a_separator_is_refused(corpus) -> None:
    rewrite = "Provisioned warehouses and loaded billing records with pytest checks on every load."

    accepted, reason, report = vet(RECORDS, rewrite, corpus)

    assert not accepted
    assert report.dropped_figures == ("1,000",)
    assert reason is not None and "'1,000'" in reason


def test_a_reworded_bullet_that_keeps_its_count_is_accepted(corpus) -> None:
    rewrite = (
        "Provisioned warehouses, loading 1,000 billing records with pytest checks on every load."
    )

    accepted, reason, report = vet(RECORDS, rewrite, corpus)

    assert accepted, reason
    assert report.dropped_figures == ()


@pytest.mark.parametrize("written", ["1000", "1k", "1,000+"])
def test_this_check_has_no_quarrel_with_another_spelling_of_the_count(written: str) -> None:
    """Asked of this check alone. Two older ones are stricter and were left
    as they are: the recombination check refuses `1000 billing` for `1,000
    billing`, and the fabrication guard refuses `1k` as a number the source
    does not have. Both keep the résumé's own bullet, which is the safe way
    to be wrong."""
    rewrite = f"Provisioned warehouses, loading {written} billing records with pytest checks."

    assert figures.lost(RECORDS, rewrite) is None


def test_a_dropped_percentage_is_refused(corpus) -> None:
    accepted, reason, report = vet(
        ERRORS, "Maintained the billing service and reduced invoice errors.", corpus
    )

    assert not accepted
    assert report.dropped_figures == ("30%",)


def test_a_percentage_that_lost_its_sign_is_a_changed_figure(corpus) -> None:
    """ "by 30" is not "by 30%". The fabrication guard reads both as 30."""
    accepted, reason, report = vet(
        ERRORS, "Maintained the billing service and reduced invoice errors by 30.", corpus
    )

    assert not accepted
    assert report.dropped_figures == ("30%",)
    assert reason is not None and reason.startswith("changes the figure '30%'")
    assert "'30'" in reason


def test_a_range_keeps_both_ends(corpus) -> None:
    kept = (
        "Cut build times from 10 to 15 minutes down to 2-3 minutes across 12 million events a day."
    )
    lost = "Cut build times to 2-3 minutes across 12 million events a day."

    assert vet(BUILDS, kept, corpus)[0], vet(BUILDS, kept, corpus)[1]

    accepted, _, report = vet(BUILDS, lost, corpus)
    assert not accepted
    assert report.dropped_figures == ("10", "15")


def test_a_number_swapped_for_another_bullets_was_already_refused(corpus) -> None:
    """12 is the résumé's, from the bullet below. The fabrication guard holds
    a number to the line it came from and refuses this before the new check
    is reached, for its own reason. Asked directly, the new check reads it as
    a changed figure."""
    rewrite = "Maintained the billing service and reduced invoice errors by 12%."

    accepted, reason, report = vet(ERRORS, rewrite, corpus)

    assert not accepted
    assert reason is not None and "unsupported" in reason
    assert report.dropped_figures == ()
    loss = figures.lost(ERRORS, rewrite)
    assert loss is not None
    assert loss.reason == "changes the figure '30%': the rewrite says '12%'"
    assert loss.loggable == "changes 1 figure the bullet states"


def test_a_rewrite_that_keeps_every_figure_is_accepted(corpus) -> None:
    rewrite = (
        "Trained a churn classifier using 148 months of billing history, "
        "with a test ROC-AUC of 0.72."
    )

    accepted, reason, report = vet(SCORE, rewrite, corpus)

    assert accepted, reason
    assert report.dropped_figures == ()


def test_a_dropped_date_is_not_this_checks_to_refuse() -> None:
    bullet = "Led the 2023 migration of 40 services, finished in March 2024."
    rewrite = "Led the migration of 40 services."

    assert figures.dropped(bullet, rewrite) == ()


# --------------------------------------------------------------------------
# What was refused before is refused the same way
# --------------------------------------------------------------------------


def test_a_dropped_technology_keeps_its_own_reason(corpus) -> None:
    """Both are gone from this rewrite. The technology check ran first before
    this one existed, and still does."""
    rewrite = "Provisioned warehouses and loaded billing records with checks on every load."

    accepted, reason, report = vet(RECORDS, rewrite, corpus)

    assert not accepted
    assert reason is not None and reason.startswith("drops 'pytest'")
    assert report.dropped_figures == ()


def test_an_invented_number_is_still_the_fabrication_guards(corpus) -> None:
    rewrite = "Maintained the billing service and reduced invoice errors by 30% for 500 clients."

    accepted, reason, _ = vet(ERRORS, rewrite, corpus)

    assert not accepted
    assert reason is not None and "unsupported" in reason and "500" in reason


# --------------------------------------------------------------------------
# What the result says, and what the log does not
# --------------------------------------------------------------------------

#: Every word is the bullet's own, so nothing before the new check refuses it.
DROPS_THE_SCORE = "Trained a churn classifier using 148 months of billing history."


async def test_the_result_names_the_figure_and_the_log_does_not(corpus, capsys) -> None:
    rewrite = await tailor_bullet(
        StubProvider({"Trained": DROPS_THE_SCORE}), SCORE, POSTING, corpus
    )

    assert rewrite.tailored == SCORE
    assert rewrite.guard_refused and not rewrite.provider_failed
    assert rewrite.dropped_figures == ["0.72"]
    assert rewrite.rejected_reason is not None and "'0.72'" in rewrite.rejected_reason

    logged = capsys.readouterr().out
    assert "tailor_rejected" in logged
    assert "0.72" not in logged
    assert "figures=1" in logged


# --------------------------------------------------------------------------
# A refused rewrite goes nowhere
# --------------------------------------------------------------------------


async def _owner(session) -> tuple[Candidate, Profile, Resume, ParsedResume]:
    suffix = uuid.uuid4().hex[:8]
    user = User(email=f"f-{suffix}@example.com")
    session.add(user)
    await session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email=f"f-{suffix}@example.com")
    session.add(candidate)
    await session.flush()
    parsed = parse_text(RESUME)
    base = Resume(
        candidate_id=candidate.id,
        version=1,
        storage_ref=f"r/{suffix}",
        parsed_json=parsed.model_dump(mode="json"),
    )
    session.add(base)
    await session.flush()
    profile = Profile(candidate_id=candidate.id, label="default", base_resume_id=base.id)
    session.add(profile)
    await session.flush()
    return candidate, profile, base, parsed


async def test_a_refused_rewrite_is_not_published_cached_attached_or_sent(db_session) -> None:
    """The whole way out. The model's rewrite drops the score; what is
    rendered, what the cache serves the next time, what an application points
    at and the file the worker would upload all hold the bullet as the résumé
    wrote it."""
    candidate, profile, _, parsed = await _owner(db_session)
    corpus = SourceCorpus.from_resume(parsed)
    _, bullets = tailorable_bullets(parsed)
    assert SCORE in bullets

    result = await tailor_bullets(StubProvider({"0.72": DROPS_THE_SCORE}), bullets, POSTING, corpus)
    refused = next(b for b in result.bullets if b.original == SCORE)
    assert refused.guard_refused and refused.dropped_figures == ["0.72"]
    assert result.provider_failures == 0

    # Published: the document is the résumé's own bullet.
    assert SCORE in apply_rewrites(parsed, result).raw_lines
    published = await publish_tailored(
        db_session,
        candidate_id=candidate.id,
        parsed=parsed,
        result=result,
        tailored_key="k-figures",
    )
    assert published is not None
    stored = ParsedResume.model_validate(published.parsed_json)
    assert SCORE in stored.raw_lines
    assert DROPS_THE_SCORE not in stored.raw_lines

    # Cached: the next run for this posting is served that same document.
    cached = await find_cached(db_session, candidate_id=candidate.id, key="k-figures")
    assert cached is not None and cached.id == published.id

    # Attached and sent: the file the worker uploads says 0.72.
    application = Application(
        candidate_id=candidate.id,
        profile_id=profile.id,
        url="https://x.test/figures",
        ats="greenhouse",
        tailored_resume_id=published.id,
    )
    db_session.add(application)
    await db_session.flush()
    path = await apply_job._resume_path(db_session, application, profile)
    assert path is not None
    assert str(path) == str(get_storage().path_for(published.storage_ref))

    sent = parse_resume(get_storage().get(published.storage_ref), "tailored.pdf")
    text = " ".join(sent.raw_lines)
    assert "0.72" in text
    assert "classifier using" not in text
