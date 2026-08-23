"""Cover letters, under the same rule as the résumé.

§2.1 was written about bullets, and it matters more here: a bullet is
skimmed, a letter is read closely and then asked about in an interview. The
tests worth having are the ones proving the letter is refused rather than
softened.
"""

from __future__ import annotations

from packages.llm.provider import StubProvider
from packages.tailor.cover import (
    MAX_WORDS,
    MIN_WORDS,
    CoverLetter,
    mentions_protected,
    vet,
    write,
)
from packages.tailor.guard import SourceCorpus

RESUME = """Jane Doe — Backend Engineer

Acme Corp — Senior Backend Engineer, 2021 to 2024
Maintained the billing service and reduced invoice errors by 30%.
Built a Python and PostgreSQL pipeline handling 12 million events a day.

Skills: Python, PostgreSQL, Docker, Kafka
"""

JOB = "We need a backend engineer with Python and PostgreSQL experience for our billing team."


def _corpus() -> SourceCorpus:
    return SourceCorpus.from_texts(RESUME)


def _letter(body: str) -> str:
    """Pad a letter to a usable length using only corpus vocabulary."""
    filler = (
        "The billing service work at Acme Corp is the closest match to this role. "
        "I maintained that service and reduced invoice errors by 30%. "
        "I built a Python and PostgreSQL pipeline handling 12 million events a day. "
    )
    text = body + " " + filler * 4
    return text


# --------------------------------------------------------------------------
# What the guard refuses
# --------------------------------------------------------------------------


def test_a_letter_inventing_a_credential_is_refused() -> None:
    accepted, reason, _ = vet(
        _letter("I am a Certified Kubernetes Administrator with AWS experience."), _corpus()
    )

    assert not accepted
    assert reason is not None


def test_a_supported_letter_is_accepted() -> None:
    accepted, reason, _ = vet(_letter("Jane Doe, backend engineer."), _corpus())

    assert accepted, reason


def test_a_letter_raising_salary_is_refused() -> None:
    """§2.2 keeps that verbatim from the profile. A letter volunteering it is
    generating exactly the answer that rule exists to prevent."""
    accepted, reason, _ = vet(
        _letter("My salary expectation is competitive for this market."), _corpus()
    )

    assert not accepted
    assert "salary" in (reason or "")


def test_a_letter_raising_sponsorship_is_refused() -> None:
    accepted, reason, _ = vet(_letter("I do not require visa sponsorship."), _corpus())

    assert not accepted
    assert reason is not None


def test_protected_terms_are_detected_individually() -> None:
    assert mentions_protected("my notice period is one month") == "notice period"
    assert mentions_protected("I hold a green card") == "green card"
    assert mentions_protected("I maintained the billing service") is None


def test_a_filler_opener_is_refused() -> None:
    """A letter that starts here has not started."""
    accepted, reason, _ = vet(_letter("I am excited to apply for this position."), _corpus())

    assert not accepted
    assert "filler" in (reason or "")


def test_a_letter_too_short_to_be_specific_is_refused() -> None:
    accepted, reason, _ = vet("Jane Doe. Backend engineer at Acme Corp.", _corpus())

    assert not accepted
    assert "short" in (reason or "")


def test_a_letter_nobody_will_read_is_refused() -> None:
    accepted, reason, _ = vet(("billing service Acme Corp " * 200), _corpus())

    assert not accepted
    assert "longer" in (reason or "")


def test_an_empty_response_is_refused() -> None:
    accepted, reason, _ = vet("", _corpus())

    assert not accepted
    assert "nothing" in (reason or "")


# --------------------------------------------------------------------------
# No fallback — the property that separates this from bullet tailoring
# --------------------------------------------------------------------------


async def test_a_refused_letter_produces_no_letter() -> None:
    """A bullet falls back to its original. A letter has no original, and the
    alternative to a bad letter is no letter."""
    provider = StubProvider(
        responses={"Write the cover letter": _letter("I hold a PMP certification from Google.")}
    )

    result = await write(
        provider, resume_text=RESUME, job_description=JOB, corpus=_corpus(), company="Acme"
    )

    assert not result.usable
    assert result.text == ""
    assert result.rejected_reason is not None


async def test_an_accepted_letter_comes_back_whole() -> None:
    body = _letter("Jane Doe has worked on billing systems.")
    provider = StubProvider(responses={"Write the cover letter": body})

    result = await write(provider, resume_text=RESUME, job_description=JOB, corpus=_corpus())

    assert result.usable
    assert result.word_count > MIN_WORDS
    assert result.word_count < MAX_WORDS


async def test_a_provider_failure_is_not_a_letter() -> None:
    class Broken(StubProvider):
        async def complete(self, system, user, *, max_tokens=1024, temperature=0.7):
            raise RuntimeError("model down")

    result = await write(Broken(), resume_text=RESUME, job_description=JOB, corpus=_corpus())

    assert not result.usable
    assert "provider error" in (result.rejected_reason or "")


async def test_the_letter_runs_at_the_cover_letter_temperature() -> None:
    """The one task §7 gives real variance to."""
    from packages.llm.router import temperature_for

    provider = StubProvider(responses={"Write the cover letter": _letter("Jane Doe.")})

    await write(provider, resume_text=RESUME, job_description=JOB, corpus=_corpus())

    assert provider.temperatures == [temperature_for("write_cover_letter")]


def test_an_unusable_letter_is_falsy_by_construction() -> None:
    assert not CoverLetter(rejected_reason="nope").usable
    assert not CoverLetter(text="", accepted=True).usable


def test_first_person_prose_is_not_read_as_proper_nouns() -> None:
    """The guard's word list was sized for résumé bullets, which are terse
    and third-person. A letter says "My" and "We" at the start of sentences,
    and reading those as proper nouns rejected nearly every letter."""
    from packages.tailor.guard import EntityKind, extract_entities

    found = {
        e.text
        for e in extract_entities("My work at Acme Corp. We shipped it. Please read.")
        if e.kind is not EntityKind.NOUN
    }

    assert found == {"Acme", "Corp"}
