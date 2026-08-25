"""Which model wrote the résumé the owner is about to approve.

§7 records `answered_by` on the provider precisely so that a résumé tailored by
llama3.1 after the Gemini allowance ran out can be told apart from one tailored
by Gemini. It was recorded and never shown: the review screen reads
`review_json["resume_diff"]`, which carried counts and a unified diff and no
model name at all.

The reuse paths are the half that matters most, and the half a naive wiring
misses. A cache hit and a batch-tailored résumé never call a provider during the
apply, so there is no live `answered_by` to read — the model has to have been
written down when the *document* was made, or the screen goes blank for exactly
the applications that skipped the call. Blank-on-reuse is the same shape of
invisibility that let the untailored-résumé bug survive every gate: the field
looks absent rather than wrong.
"""

from __future__ import annotations

import uuid

import pytest

from packages.core.models import (
    Application,
    Candidate,
    Match,
    Posting,
    Profile,
    Resume,
    User,
)
from packages.core.storage import get_storage, resume_key
from packages.tailor.parse import Contact, ParsedResume
from packages.tailor.rewrite import BulletRewrite, TailorResult

JOB = "Backend engineer. Python services, Postgres, and queueing."


async def _candidate(db_session) -> Candidate:
    suffix = uuid.uuid4().hex[:8]
    user = User(email=f"o-{suffix}@example.com")
    db_session.add(user)
    await db_session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email=f"c-{suffix}@example.com")
    db_session.add(candidate)
    await db_session.flush()
    return candidate


async def _base_resume(db_session, candidate: Candidate) -> Resume:
    resume = Resume(
        candidate_id=candidate.id,
        version=1,
        storage_ref=f"resumes/{candidate.id}/v1/r.txt",
        parsed_json={
            "raw_lines": ["Experience", "Built backend services in Python."],
            "sections": {"experience": ["Built backend services in Python."]},
        },
    )
    db_session.add(resume)
    await db_session.flush()
    return resume


async def _application(db_session, candidate: Candidate, profile: Profile) -> Application:
    application = Application(
        candidate_id=candidate.id,
        profile_id=profile.id,
        url=f"https://x.test/{uuid.uuid4().hex[:8]}",
        ats="greenhouse",
    )
    db_session.add(application)
    await db_session.flush()
    return application


class _Provider:
    """A provider whose `answered_by` changes when it is called.

    This is the whole point of reading it late. `FallbackProvider` sets
    `answered_by` back to the primary at the top of every call and only rewrites
    it once the primary has actually failed — so a caller that snapshots the
    attribute *before* tailoring records the model that did not answer.
    """

    name = "gemini"
    model = "gemini-2.0-flash"

    def __init__(self, falls_back_to: str | None = None) -> None:
        self.answered_by = self.name
        self._falls_back_to = falls_back_to

    async def complete(self, system: str, user: str, **kwargs: object) -> str:
        if self._falls_back_to:
            self.answered_by = self._falls_back_to
        return "Built and scaled backend services in Python."

    async def complete_json(self, system: str, user: str, schema: type) -> object:
        raise NotImplementedError


def _parsed() -> ParsedResume:
    return ParsedResume(
        contact=Contact(name="Owner", email="owner@example.com"),
        sections={"experience": ["Built backend services in Python."]},
        raw_lines=["Built backend services in Python."],
    )


def _result() -> TailorResult:
    return TailorResult(
        bullets=[
            BulletRewrite(
                original="Built backend services in Python.",
                tailored="Built and scaled backend services in Python.",
                changed=True,
            )
        ]
    )


@pytest.mark.asyncio
async def test_publish_writes_down_the_model_that_wrote_the_document(
    db_session, monkeypatch
) -> None:
    """The document, not the run, is what carries the answer.

    Stored here rather than derived at read time because the run that tailored
    it is long gone by the time a cache hit serves this row to another
    application.
    """
    from packages.tailor import publish as publish_mod

    monkeypatch.setattr(publish_mod, "assemble_pdf", lambda *a, **k: b"%PDF-1.4 tailored")
    candidate = await _candidate(db_session)

    published = await publish_mod.publish_tailored(
        db_session,
        candidate_id=candidate.id,
        parsed=_parsed(),
        result=_result(),
        answered_by="ollama:llama3.1",
    )

    assert published is not None
    assert published.tailored_by == "ollama:llama3.1"


@pytest.mark.asyncio
async def test_publish_leaves_it_null_when_the_caller_did_not_say(db_session, monkeypatch) -> None:
    """A guess here is worse than a blank. NULL means "unrecorded", not a model."""
    from packages.tailor import publish as publish_mod

    monkeypatch.setattr(publish_mod, "assemble_pdf", lambda *a, **k: b"%PDF-1.4 tailored")
    candidate = await _candidate(db_session)

    published = await publish_mod.publish_tailored(
        db_session,
        candidate_id=candidate.id,
        parsed=_parsed(),
        result=_result(),
    )

    assert published is not None
    assert published.tailored_by is None


@pytest.mark.asyncio
async def test_a_fresh_tailoring_reports_the_model_to_the_review_screen(
    db_session, monkeypatch
) -> None:
    from apps.worker import apply_job

    candidate = await _candidate(db_session)
    resume = await _base_resume(db_session, candidate)
    profile = Profile(candidate_id=candidate.id, label="p", base_resume_id=resume.id)
    db_session.add(profile)
    await db_session.flush()
    application = await _application(db_session, candidate, profile)

    handed: dict[str, object] = {}

    async def _capture(session, **kwargs):
        handed.update(kwargs)
        return None

    monkeypatch.setattr("packages.tailor.publish.publish_tailored", _capture)
    monkeypatch.setattr(apply_job.llm_router, "tailor_resume", lambda: _Provider())

    posting = Posting(url=f"https://x.test/p/{uuid.uuid4().hex[:8]}", description_raw=JOB)
    db_session.add(posting)
    await db_session.flush()

    diff = await apply_job._tailor(db_session, application, profile, posting)

    assert diff is not None
    assert diff["answered_by"] == "gemini"
    assert handed["answered_by"] == "gemini"


@pytest.mark.asyncio
async def test_the_model_is_read_after_the_call_not_before(db_session, monkeypatch) -> None:
    """A fallback is invisible if `answered_by` is snapshotted too early.

    This is the case the whole field exists for: the allowance ran out mid-run,
    llama3.1 answered, and the owner is looking at a different document from the
    one they would have got an hour earlier.
    """
    from apps.worker import apply_job

    candidate = await _candidate(db_session)
    resume = await _base_resume(db_session, candidate)
    profile = Profile(candidate_id=candidate.id, label="p", base_resume_id=resume.id)
    db_session.add(profile)
    await db_session.flush()
    application = await _application(db_session, candidate, profile)

    handed: dict[str, object] = {}

    async def _capture(session, **kwargs):
        handed.update(kwargs)
        return None

    monkeypatch.setattr("packages.tailor.publish.publish_tailored", _capture)
    monkeypatch.setattr(
        apply_job.llm_router,
        "tailor_resume",
        lambda: _Provider(falls_back_to="ollama:llama3.1"),
    )

    posting = Posting(url=f"https://x.test/p/{uuid.uuid4().hex[:8]}", description_raw=JOB)
    db_session.add(posting)
    await db_session.flush()

    diff = await apply_job._tailor(db_session, application, profile, posting)

    assert diff is not None
    assert diff["answered_by"] == "ollama:llama3.1", (
        "reported the primary; answered_by was read before the call, so a "
        "fallback to the local model is invisible on the review screen"
    )


@pytest.mark.asyncio
async def test_a_batch_prepared_resume_still_names_its_model(db_session, monkeypatch) -> None:
    """The reuse path that serves an overnight batch run.

    No provider is built during this apply, so the only possible source is the
    row written when the batch tailored it.
    """
    from apps.worker import apply_job

    candidate = await _candidate(db_session)
    resume = await _base_resume(db_session, candidate)
    profile = Profile(candidate_id=candidate.id, label="p", base_resume_id=resume.id)
    db_session.add(profile)
    await db_session.flush()

    posting = Posting(url=f"https://x.test/p/{uuid.uuid4().hex[:8]}", description_raw=JOB)
    db_session.add(posting)
    await db_session.flush()

    storage = get_storage()
    key = resume_key(str(candidate.id), 2, "tailored.pdf")
    storage.put(key, b"%PDF-1.4 tailored")
    tailored = Resume(
        candidate_id=candidate.id,
        version=2,
        storage_ref=key,
        parsed_json={},
        tailored_by="gemini",
    )
    db_session.add(tailored)
    await db_session.flush()

    application = await _application(db_session, candidate, profile)
    application.posting_id = posting.id
    db_session.add(
        Match(
            profile_id=profile.id,
            posting_id=posting.id,
            score=0.9,
            tailored_resume_id=tailored.id,
        )
    )
    await db_session.flush()

    diff = await apply_job._tailor(db_session, application, profile, posting)

    assert diff is not None
    assert diff["reused"] is True
    assert diff["answered_by"] == "gemini", "a reused résumé cannot say which model wrote it"
