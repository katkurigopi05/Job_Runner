"""Work authorization: what a posting said, and what nobody may infer from it.

Every case below was chosen because the previous implementation got it wrong or
could not answer it at all. Measured against `filters._SPONSORSHIP_RE` as it
stood, on the seven phrasings this suite was asked to cover:

    "We do not offer visa sponsorship."                    kept      (wrong)
    "We cannot sponsor now or in the future."              kept      (wrong)
    "Visa sponsorship is available."                       kept      (right)
    "Candidates with or without sponsorship ... apply."    EXCLUDED  (wrong)
    "US citizens only."                                    kept, unflagged
    "US citizens or permanent residents only."             kept, unflagged
    (a posting that says nothing)                          kept      (right)

Two of those are false negatives — the pattern list matched literal phrases and
never learned negation, so the commonest English form of a refusal read as
silence. One is a false positive, and it is the expensive kind: `without
sponsorship` is a substring of `with or without sponsorship`, a phrase that
exists precisely to say sponsorship is not a barrier. The last two were not
sponsorship statements at all and had no filter to reach.
"""

from __future__ import annotations

import uuid

import pytest

from packages.core.enums import CitizenshipStatus
from packages.core.models import Posting, Profile
from packages.matching.eligibility import (
    UNKNOWN_LABEL,
    Citizenship,
    Sponsorship,
    read_posting,
)
from packages.matching.filters import (
    apply_filters,
    citizenship_ok,
    eligibility_of,
    sponsorship_ok,
)


def _profile(**kwargs) -> Profile:
    defaults = dict(
        id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        label="backend",
        location="San Francisco, CA",
        work_auth="US citizen",
        needs_sponsorship=False,
        citizenship_status=None,
        links_json={},
        answers_kv_json={},
    )
    defaults.update(kwargs)
    return Profile(**defaults)


def _posting(description: str) -> Posting:
    return Posting(
        id=uuid.uuid4(),
        url=f"https://example.com/{uuid.uuid4()}",
        title="Backend Engineer",
        location="San Francisco, CA",
        description_raw=description,
    )


# --------------------------------------------------------------------------
# The seven cases this was asked to cover
# --------------------------------------------------------------------------

NEEDS_SPONSORSHIP = "We do not offer visa sponsorship."
NEVER_SPONSORS = "We cannot sponsor now or in the future."
SPONSORS = "Visa sponsorship is available."
EITHER_WAY = "Candidates with or without sponsorship requirements may apply."
CITIZENS = "US citizens only."
CITIZENS_OR_RESIDENTS = "US citizens or permanent residents only."
SILENT = "Join a growing team building analytics infrastructure in Python."


@pytest.mark.parametrize("text", [NEEDS_SPONSORSHIP, NEVER_SPONSORS])
def test_a_negated_refusal_is_a_refusal(text: str) -> None:
    """The false negatives. `do not offer` and `cannot` were both invisible."""
    assert read_posting(text).sponsorship is Sponsorship.UNAVAILABLE
    assert not sponsorship_ok(_profile(needs_sponsorship=True), _posting(text))


def test_an_offer_is_read_as_an_offer_and_never_excludes() -> None:
    assert read_posting(SPONSORS).sponsorship is Sponsorship.AVAILABLE
    assert sponsorship_ok(_profile(needs_sponsorship=True), _posting(SPONSORS))


def test_with_or_without_sponsorship_is_ambiguous_not_a_refusal() -> None:
    """The false positive, and the costly one.

    This phrasing is a posting going out of its way to say sponsorship is not
    the deciding factor. Excluding on it hides exactly the roles the owner most
    wants — and it did, because `without sponsorship` is inside it.
    """
    read = read_posting(EITHER_WAY)
    assert read.sponsorship is Sponsorship.AMBIGUOUS
    assert sponsorship_ok(_profile(needs_sponsorship=True), _posting(EITHER_WAY))
    assert "without resolving" in read.summary()


def test_citizens_only_is_read_and_is_not_a_sponsorship_statement() -> None:
    """A restriction sponsorship cannot fix, so it is not filed under it."""
    read = read_posting(CITIZENS)
    assert read.citizenship is Citizenship.CITIZENS_ONLY
    assert read.sponsorship is Sponsorship.UNSTATED, "it said nothing about sponsoring"


def test_citizens_or_residents_is_a_different_restriction() -> None:
    """Read as citizens-only, a green-card holder is excluded from a job that
    names them explicitly. The two verdicts are separate for that one reason."""
    assert read_posting(CITIZENS_OR_RESIDENTS).citizenship is (
        Citizenship.CITIZENS_OR_RESIDENTS_ONLY
    )


def test_silence_is_not_confirmation_in_either_direction() -> None:
    """A posting that says nothing has said nothing.

    Not "sponsorship available", not "no sponsorship", and — the failure this
    is really guarding — not something a screen may render as a blank line the
    reader takes for "no restrictions".
    """
    read = read_posting(SILENT)
    assert read.sponsorship is Sponsorship.UNSTATED
    assert read.citizenship is Citizenship.UNSTATED
    assert read.certain is False
    assert read.summary() == UNKNOWN_LABEL
    assert sponsorship_ok(_profile(needs_sponsorship=True), _posting(SILENT))
    assert citizenship_ok(_profile(citizenship_status="not_authorized"), _posting(SILENT))


# --------------------------------------------------------------------------
# Phrasings a real posting uses
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "We are unable to sponsor visas at this time.",
        "This position is not eligible for visa sponsorship.",
        "Sponsorship is not available for this role.",
        "No visa sponsorship is provided.",
        "Applicants must be authorized to work in the US without the need for sponsorship.",
        "We do not provide employment sponsorship.",
        "Candidates must be able to work without employer sponsorship.",
    ],
)
def test_real_refusals_are_caught(text: str) -> None:
    assert read_posting(text).sponsorship is Sponsorship.UNAVAILABLE, text


@pytest.mark.parametrize(
    "text",
    [
        "Visa sponsorship is available for this role.",
        "We will sponsor H-1B visas for exceptional candidates.",
        "We are happy to sponsor the right candidate.",
        "Sponsorship offered.",
    ],
)
def test_real_offers_are_caught(text: str) -> None:
    assert read_posting(text).sponsorship is Sponsorship.AVAILABLE, text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("U.S. citizens only.", Citizenship.CITIZENS_ONLY),
        ("This role is restricted to US citizens.", Citizenship.CITIZENS_ONLY),
        ("Must be a US citizen.", Citizenship.CITIZENS_ONLY),
        ("US citizenship required.", Citizenship.CITIZENS_ONLY),
        ("Must be a U.S. Citizen or Green Card holder.", Citizenship.CITIZENS_OR_RESIDENTS_ONLY),
        (
            "Open to US citizens and permanent residents.",
            Citizenship.CITIZENS_OR_RESIDENTS_ONLY,
        ),
        ("We hire globally and value every background.", Citizenship.UNSTATED),
        ("Our citizens' advice partnership serves the community.", Citizenship.UNSTATED),
    ],
)
def test_citizenship_is_read_from_what_the_posting_says(text: str, expected) -> None:
    assert read_posting(text).citizenship is expected, text


def test_an_abbreviated_us_does_not_split_the_sentence() -> None:
    """`U.S.` ends a clause for a naive splitter, and that changed the verdict.

    `Must be a U.S. Citizen or Green Card holder.` split into `Must be a U.S.`
    and `Citizen or Green Card holder.`, so the citizens-and-residents rule —
    which needs both halves in one clause — never fired and the posting read as
    citizens-only. A permanent resident would have been excluded from a job
    that names them.
    """
    read = read_posting("Must be a U.S. Citizen or Green Card holder.")
    assert read.citizenship is Citizenship.CITIZENS_OR_RESIDENTS_ONLY
    resident = _profile(citizenship_status=CitizenshipStatus.PERMANENT_RESIDENT.value)
    assert citizenship_ok(resident, _posting("Must be a U.S. Citizen or Green Card holder."))


def test_a_posting_that_refuses_and_offers_is_ambiguous_not_a_refusal() -> None:
    """A contradiction is not evidence, and a hard filter needs evidence."""
    text = "We do not sponsor visas. Visa sponsorship is available for senior roles."
    assert read_posting(text).sponsorship is Sponsorship.AMBIGUOUS
    assert sponsorship_ok(_profile(needs_sponsorship=True), _posting(text))


def test_negation_does_not_reach_across_a_sentence() -> None:
    """`We cannot match every salary.` must not make the next sentence a refusal."""
    text = "We cannot match every salary expectation. Visa sponsorship is available."
    assert read_posting(text).sponsorship is Sponsorship.AVAILABLE


# --------------------------------------------------------------------------
# Who each restriction actually excludes
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "kept"),
    [
        (CitizenshipStatus.US_CITIZEN.value, True),
        (CitizenshipStatus.PERMANENT_RESIDENT.value, False),
        (CitizenshipStatus.OTHER_AUTHORIZED.value, False),
        (CitizenshipStatus.NOT_AUTHORIZED.value, False),
        (None, True),
    ],
)
def test_citizens_only_excludes_everyone_who_is_not_one(status: str | None, kept: bool) -> None:
    """NULL is kept on purpose: excluding on a field nobody filled in hides
    real jobs, and the restriction is surfaced instead of acted on."""
    assert citizenship_ok(_profile(citizenship_status=status), _posting(CITIZENS)) is kept


@pytest.mark.parametrize(
    ("status", "kept"),
    [
        (CitizenshipStatus.US_CITIZEN.value, True),
        (CitizenshipStatus.PERMANENT_RESIDENT.value, True),
        (CitizenshipStatus.OTHER_AUTHORIZED.value, False),
        (None, True),
    ],
)
def test_citizens_or_residents_admits_a_resident(status: str | None, kept: bool) -> None:
    assert (
        citizenship_ok(_profile(citizenship_status=status), _posting(CITIZENS_OR_RESIDENTS)) is kept
    )


def test_the_two_facts_do_not_decide_each_other() -> None:
    """One boolean was answering both questions. These are the cases that shows.

    A permanent resident needs no sponsorship and still fails citizens-only; a
    candidate who needs sponsorship is untouched by a citizens-*or-residents*
    posting unless it also refuses to sponsor.
    """
    resident = _profile(
        needs_sponsorship=False,
        citizenship_status=CitizenshipStatus.PERMANENT_RESIDENT.value,
    )
    assert sponsorship_ok(resident, _posting(CITIZENS)), "no sponsorship statement to fail"
    assert not citizenship_ok(resident, _posting(CITIZENS))

    needs = _profile(needs_sponsorship=True, citizenship_status=None)
    assert citizenship_ok(needs, _posting(NEEDS_SPONSORSHIP)), "no citizenship statement"
    assert not sponsorship_ok(needs, _posting(NEEDS_SPONSORSHIP))


def test_apply_filters_reports_each_restriction_separately() -> None:
    text = f"{CITIZENS} {NEEDS_SPONSORSHIP}"
    result = apply_filters(
        _profile(
            needs_sponsorship=True,
            citizenship_status=CitizenshipStatus.NOT_AUTHORIZED.value,
        ),
        _posting(text),
    )
    assert not result.passed
    assert any("cannot sponsor" in reason for reason in result.reasons)
    assert any("restricted to" in reason for reason in result.reasons)


# --------------------------------------------------------------------------
# What the owner is shown
# --------------------------------------------------------------------------


def test_evidence_quotes_the_posting_rather_than_asserting() -> None:
    read = read_posting("Nice team. We do not offer visa sponsorship. Apply today.")
    assert [e.claim for e in read.evidence] == ["sponsorship_unavailable"]
    assert read.evidence[0].quote == "We do not offer visa sponsorship."


def test_evidence_is_truncated_rather_than_copying_the_posting() -> None:
    from packages.matching.eligibility import MAX_EVIDENCE_CHARS

    read = read_posting("We do not offer visa sponsorship " + "x" * 5_000 + ".")
    assert read.evidence
    assert len(read.evidence[0].quote) <= MAX_EVIDENCE_CHARS


def test_the_wire_form_always_carries_a_summary() -> None:
    """A consumer must never have to infer from an absent field. `unstated` is
    a value, not a missing key, and the summary is a sentence not a blank."""
    payload = eligibility_of(_posting(SILENT)).as_dict()
    assert payload["sponsorship"] == "unstated"
    assert payload["citizenship"] == "unstated"
    assert payload["certain"] is False
    assert payload["summary"] == UNKNOWN_LABEL
    assert payload["evidence"] == []


def test_nothing_claims_opt_or_e_verify_or_a_sponsorship_history() -> None:
    """The inference this must never make.

    None of OPT acceptance, STEM-OPT, E-Verify participation or past H-1B
    sponsorship follows from a posting not mentioning visas, and a feed that
    labelled a job "OPT-friendly" on that basis would be inventing the one fact
    an applicant cannot afford to be wrong about.
    """
    from pathlib import Path

    source = Path("packages/matching/eligibility.py").read_text(encoding="utf-8")
    verdicts = {*Sponsorship, *Citizenship}
    assert not {v for v in verdicts if "opt" in v.value or "verify" in v.value}

    payload = eligibility_of(_posting(SILENT)).as_dict()
    rendered = str(payload).lower()
    for claim in ("opt-friendly", "stem-opt", "e-verify", "h-1b sponsor"):
        assert claim not in rendered

    # The words appear in the module only where it explains that it will not
    # conclude them — never as an output value.
    assert "opt-friendly" not in {v.value for v in verdicts}
    assert "OPT" in source, "the refusal to infer it is documented at the source"


def test_the_exclusion_reason_states_only_what_it_excluded_on() -> None:
    """`restriction()` exists so the reason is not two facts joined by a semicolon.

    Built from `summary()` it read "posting is restricted to US citizens only;
    sponsorship: unknown — verify with employer", which names a thing the
    filter did *not* act on inside a sentence about what it did.
    """
    from packages.matching.eligibility import read_posting as read

    assert read(CITIZENS).restriction() == "US citizens only"
    assert read(CITIZENS_OR_RESIDENTS).restriction() == "US citizens or permanent residents only"
    assert read(SILENT).restriction() is None

    result = apply_filters(
        _profile(citizenship_status=CitizenshipStatus.NOT_AUTHORIZED.value),
        _posting(CITIZENS),
    )
    assert result.reasons == ["posting is restricted to US citizens only"]
