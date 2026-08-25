"""The cover letter that was written but never sent.

Phase 3 built `packages/tailor/cover.py`: it drafts a letter, sifts the
sentences that do not trace to the résumé, vets what is left through the
fabrication guard, and refuses outright rather than falling back. Nothing
called it. `Application.cover_letter_ref` was in the schema from the first
migration and written by nobody, so no application has ever carried a letter
— the same shape of gap as the tailored résumé that was never uploaded, and
invisible for the same reason: no test asserted what reaches the form.

These assert the wiring rather than the writer, which `test_cover_letter.py`
already covers:

- a letter is written only when the employer asks for one, because a provider
  call for a form with no such field is money spent on nothing;
- a refused letter leaves no file and records why, because §2.1 offers no
  fallback here and a silent refusal is indistinguishable from never trying;
- the letter is written before the form is filled.
"""

from __future__ import annotations

import uuid

import pytest

from apps.worker import apply_job
from packages.ats.base import ParsedPosting, Question, QuestionKind
from packages.core.models import Application, Candidate, Profile, Resume, User
from packages.core.storage import get_storage, resume_key
from packages.llm import router as llm_router
from packages.llm.provider import StubProvider
from packages.tailor.parse import parse_text

RESUME = """Jane Doe — Backend Engineer
jane@example.com

Experience

Acme Corp — Senior Backend Engineer, 2021 to 2024
Maintained the billing service and reduced invoice errors by 30%.
Built a Python and PostgreSQL pipeline handling 12 million events a day.

Skills

Python, PostgreSQL, Docker, Kafka
"""

JOB = (
    "We need a backend engineer with Python and PostgreSQL experience "
    "for our billing team. You will maintain services at scale."
)


def _supported_letter() -> str:
    """A letter saying only what that résumé entry already supports."""
    body = (
        "The billing service work at Acme Corp is the closest match to this role. "
        "I maintained that billing service and reduced invoice errors by 30%. "
        "I built a Python and PostgreSQL pipeline handling 12 million events a day. "
    )
    return "Dear Hiring Manager,\n\n" + body * 5 + "\n\nSincerely,\nJane Doe"


def _fabricating_letter() -> str:
    """Same shape, with claims the résumé cannot back."""
    body = (
        "I am a Certified Kubernetes Administrator with eight years at Initech. "
        "I led the migration to AWS Lambda and hold a PhD from Stanford. "
    )
    return "Dear Hiring Manager,\n\n" + body * 8 + "\n\nSincerely,\nJane Doe"


def _cover_letter_question() -> Question:
    return Question(key="cover_letter", label="Cover Letter", kind=QuestionKind.COVER_LETTER)


def _posting() -> ParsedPosting:
    return ParsedPosting(title="Backend Engineer", company="Globex", description_raw=JOB)


async def _setup(db_session) -> tuple[Application, Profile]:
    suffix = uuid.uuid4().hex[:8]
    user = User(email=f"u-{suffix}@example.com")
    db_session.add(user)
    await db_session.flush()

    candidate = Candidate(user_id=user.id, name="Jane Doe", email=f"u-{suffix}@example.com")
    db_session.add(candidate)
    await db_session.flush()

    key = resume_key(str(candidate.id), 1, "resume.pdf")
    get_storage().put(key, b"%PDF-1.4 base")
    resume = Resume(
        candidate_id=candidate.id,
        version=1,
        storage_ref=key,
        parsed_json=parse_text(RESUME).model_dump(mode="json"),
    )
    db_session.add(resume)
    await db_session.flush()

    profile = Profile(candidate_id=candidate.id, label="default", base_resume_id=resume.id)
    db_session.add(profile)
    await db_session.flush()

    application = Application(
        candidate_id=candidate.id,
        profile_id=profile.id,
        url=f"https://x.test/{suffix}",
        ats="greenhouse",
    )
    db_session.add(application)
    await db_session.flush()

    return application, profile


@pytest.fixture
def stub(monkeypatch):
    """Install a canned writer and hand the test the provider to assert on."""

    def _install(response: str) -> StubProvider:
        provider = StubProvider(responses={"Write the cover letter": response})
        monkeypatch.setattr(llm_router, "write_cover_letter", lambda: provider)
        return provider

    return _install


# --------------------------------------------------------------------------
# When a letter is written at all
# --------------------------------------------------------------------------


async def test_no_letter_is_written_when_the_form_does_not_ask_for_one(db_session, stub) -> None:
    """A provider call for a field that does not exist is money for nothing."""
    application, profile = await _setup(db_session)
    provider = stub(_supported_letter())

    questions = [Question(key="first_name", label="First Name", kind=QuestionKind.TEXT)]
    outcome = await apply_job._cover_letter(db_session, application, profile, _posting(), questions)

    assert outcome is None
    assert provider.calls == [], "asked a model for a letter nobody wanted"
    assert application.cover_letter_ref is None


async def test_a_vetted_letter_is_stored_and_referenced(db_session, stub) -> None:
    """The gap this file exists for: a letter that reaches the application."""
    application, profile = await _setup(db_session)
    stub(_supported_letter())

    outcome = await apply_job._cover_letter(
        db_session, application, profile, _posting(), [_cover_letter_question()]
    )

    assert outcome is not None
    assert outcome["accepted"] is True, outcome.get("rejected_reason")
    assert application.cover_letter_ref is not None
    assert get_storage().path_for(application.cover_letter_ref).is_file()
    assert outcome["text"].strip()


async def test_a_refused_letter_leaves_no_file_and_records_why(db_session, stub) -> None:
    """§2.1 has no fallback here — the alternative to a bad letter is none."""
    application, profile = await _setup(db_session)
    stub(_fabricating_letter())

    outcome = await apply_job._cover_letter(
        db_session, application, profile, _posting(), [_cover_letter_question()]
    )

    assert outcome is not None
    assert outcome["accepted"] is False
    assert outcome["rejected_reason"], "refused without saying why"
    assert application.cover_letter_ref is None
    assert not outcome.get("text")


async def test_a_provider_failure_does_not_stop_the_application(db_session, monkeypatch) -> None:
    """An application with a filled form and no letter beats no application."""
    application, profile = await _setup(db_session)

    class Broken(StubProvider):
        async def complete(self, *a, **kw):  # type: ignore[override]
            raise RuntimeError("provider down")

    monkeypatch.setattr(llm_router, "write_cover_letter", lambda: Broken())

    outcome = await apply_job._cover_letter(
        db_session, application, profile, _posting(), [_cover_letter_question()]
    )

    assert outcome is not None
    assert outcome["accepted"] is False
    assert application.cover_letter_ref is None


async def test_nothing_is_written_without_a_posting_description(db_session, stub) -> None:
    """A letter about a job description we never read is a letter about nothing."""
    application, profile = await _setup(db_session)
    provider = stub(_supported_letter())
    blank = ParsedPosting(title="Backend Engineer", company="Globex", description_raw="")

    outcome = await apply_job._cover_letter(
        db_session, application, profile, blank, [_cover_letter_question()]
    )

    assert outcome is None
    assert provider.calls == []


async def test_a_file_upload_field_also_counts_as_asking(db_session, stub) -> None:
    """Greenhouse offers "Attach" and "Enter manually" for the same letter."""
    application, profile = await _setup(db_session)
    stub(_supported_letter())
    attach = Question(key="cover_letter", label="Cover Letter", kind=QuestionKind.FILE)

    outcome = await apply_job._cover_letter(db_session, application, profile, _posting(), [attach])

    assert outcome is not None
    assert outcome["accepted"] is True, outcome.get("rejected_reason")
    assert application.cover_letter_ref is not None


async def test_a_resumed_run_sends_the_letter_the_owner_approved(db_session, stub) -> None:
    """Approval is of a specific letter, not of the idea of one.

    The owner approves at review and the pipeline replays. Writing a second
    letter there would spend another provider call to produce different prose
    — every real provider does — so the owner would have read one letter and
    the employer would receive another.
    """
    application, profile = await _setup(db_session)
    stub(_supported_letter())

    first = await apply_job._cover_letter(
        db_session, application, profile, _posting(), [_cover_letter_question()]
    )
    assert first is not None and first["accepted"] is True, first
    application.review_json = {"cover_letter": first}
    ref = application.cover_letter_ref

    provider = stub("something else entirely")
    second = await apply_job._cover_letter(
        db_session, application, profile, _posting(), [_cover_letter_question()]
    )

    assert second is not None
    assert second["reused"] is True
    assert second["text"] == first["text"]
    assert application.cover_letter_ref == ref
    assert provider.calls == [], "rewrote a letter the owner had already approved"


async def test_a_vanished_letter_file_is_rewritten_rather_than_referenced(db_session, stub) -> None:
    """A ref pointing at nothing must not be reused as if it were a letter."""
    application, profile = await _setup(db_session)
    stub(_supported_letter())

    first = await apply_job._cover_letter(
        db_session, application, profile, _posting(), [_cover_letter_question()]
    )
    assert first is not None
    application.review_json = {"cover_letter": first}
    get_storage().path_for(application.cover_letter_ref).unlink()

    provider = stub(_supported_letter())
    second = await apply_job._cover_letter(
        db_session, application, profile, _posting(), [_cover_letter_question()]
    )

    assert second is not None
    assert not second.get("reused")
    assert provider.calls, "reused a letter whose file is gone"
    assert get_storage().path_for(application.cover_letter_ref).is_file()


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------


def test_the_letter_is_written_before_the_form_is_filled() -> None:
    """The tailored-résumé defect, one field over.

    A letter generated after `adapter.fill` is a letter no employer receives,
    and every gate would still pass. Read the source rather than trusting the
    order to hold.
    """
    import inspect

    source = inspect.getsource(apply_job._run_pipeline)
    letter_at = source.index("_cover_letter(")
    fill_at = source.index("adapter.fill(")

    assert letter_at < fill_at, "adapter.fill runs before _cover_letter; the field goes empty"


def test_the_letter_is_passed_to_the_answer_builder() -> None:
    """Writing a letter and not handing it to the form is the same bug again."""
    import inspect

    source = inspect.getsource(apply_job._run_pipeline)

    assert "cover_letter_text=" in source or "cover_letter_path=" in source, (
        "the letter is written and never given to build_answers"
    )


# --------------------------------------------------------------------------
# Which model wrote it
# --------------------------------------------------------------------------


class _FallsBack:
    """Answers, but not with the model that was asked for.

    §7's `LLM_FALLBACK_LOCAL` does exactly this when the remote allowance is
    spent: the call succeeds, and a different model produced the text.
    """

    name = "gemini"
    model = "gemini-3.6-flash"

    def __init__(self, text: str) -> None:
        self.answered_by = self.name
        self._text = text

    async def complete(self, system: str, user: str, **kwargs: object) -> str:
        self.answered_by = "ollama:llama3.1"
        return self._text

    async def complete_json(self, system: str, user: str, schema: type) -> object:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_the_letter_records_the_model_that_actually_wrote_it(db_session, monkeypatch) -> None:
    """A letter goes to an employer under the owner's name.

    A tailored résumé records this on its own row (`resumes.tailored_by`); a
    letter has no row, so it rides in the review record beside the text it
    describes. Read after the call, never before — `FallbackProvider` rewrites
    `answered_by` only once the primary has failed, so an early read names the
    model that did not answer.
    """
    application, profile = await _setup(db_session)
    provider = _FallsBack(_supported_letter())
    monkeypatch.setattr(llm_router, "write_cover_letter", lambda: provider)

    outcome = await apply_job._cover_letter(
        db_session, application, profile, _posting(), [_cover_letter_question()]
    )

    assert outcome is not None
    assert outcome["accepted"] is True
    assert outcome["answered_by"] == "ollama:llama3.1", (
        "reported the provider that was asked rather than the model that "
        "answered, so a fallback is invisible at approval time"
    )


@pytest.mark.asyncio
async def test_a_refused_letter_still_says_which_model_wrote_it(db_session, monkeypatch) -> None:
    """A refusal is a result about a model, so it has to name the model.

    "The guard refused it" reads differently depending on whether the local
    model or the cloud one produced the draft.
    """
    application, profile = await _setup(db_session)
    provider = _FallsBack(_fabricating_letter())
    monkeypatch.setattr(llm_router, "write_cover_letter", lambda: provider)

    outcome = await apply_job._cover_letter(
        db_session, application, profile, _posting(), [_cover_letter_question()]
    )

    assert outcome is not None
    assert outcome["accepted"] is False
    assert outcome["answered_by"] == "ollama:llama3.1"
