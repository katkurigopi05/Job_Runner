"""`TAILOR_MODE` through its three callers: apply, the nightly batch, Compare models.

`tests/test_tailor_dispatch.py` holds `dispatch.tailor` on its own. This file
is what the first draft of the callers got wrong, each found by reading the
change and none by a failing test, because the tests that came with it either
stubbed the caller whole or passed without reaching the line:

- **A document refused for its PDF stayed in the cache.** `publish_tailored`
  writes the cache key before anything has read the file. Apply refused to
  attach the document, and the next run for the same posting was handed it as
  a cache hit and attached it. The batch and the comparison had the same hole.
- **Compare built two providers and read the wrong one.** One to learn the
  name, a second to make the calls, and the first was asked which model had
  answered. With the local fallback on and the allowance spent, that is a
  local model's bullet under a cloud model's name (§7).
- **A document no model wrote was signed by one.** A targeted run with no
  bullet to send still publishes when there is a project to add.
- **The default mode did not behave as before.** Apply built a provider
  before looking for a résumé the batch had prepared, and dropped the prepared
  one unless its cache key matched the key for today's provider.
- **Under `targeted` the batch read the same fifty every night.** A posting
  with nothing to gain stays pending, and most have nothing to gain.
- **A refused PDF's missing figures went to the log** (§10).

Offline. The provider is a stub and the PDFs are rendered for real.
"""

from __future__ import annotations

import uuid

import pytest
import structlog
from pydantic import ValidationError

from apps.worker import apply_job
from packages.core.config import Settings, get_settings
from packages.core.enums import TailorMode
from packages.core.models import Application, Candidate, Match, Posting, Profile, Project, Resume
from packages.core.models import User as Owner
from packages.core.storage import get_storage
from packages.llm.prompts import TAILOR_SYSTEM
from packages.tailor import batch, cache, dispatch
from packages.tailor import compare as compare_mod
from packages.tailor.bullets import tailorable_bullets
from packages.tailor.cache import TAILORING_REVISION, find_cached, mode_of, tailoring_key
from packages.tailor.targeted import NO_GAIN
from packages.tailor.verify import PdfCheck
from tests.test_targeted_tailoring import ACCEPTED, LOADER, POSTING, RESUME, Scripted

pytestmark = pytest.mark.asyncio

#: Asks for nothing the fixture résumé could gain: it states all three.
NOTHING_TO_GAIN = "Requirements:\nClassification, Python, and pandas.\n"
GAINED = "automated testing"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


async def _owner(session) -> tuple[Profile, Resume]:
    suffix = uuid.uuid4().hex[:8]
    user = Owner(email=f"o-{suffix}@example.com")
    session.add(user)
    await session.flush()
    candidate = Candidate(user_id=user.id, name="Fixture Owner", email=f"c-{suffix}@example.com")
    session.add(candidate)
    await session.flush()
    resume = Resume(
        candidate_id=candidate.id,
        version=1,
        storage_ref=f"resumes/{candidate.id}/v1/base.pdf",
        parsed_json=RESUME.model_dump(mode="json"),
    )
    session.add(resume)
    await session.flush()
    profile = Profile(candidate_id=candidate.id, label="default", base_resume_id=resume.id)
    session.add(profile)
    await session.flush()
    return profile, resume


async def _posting(session, text: str = POSTING) -> Posting:
    suffix = uuid.uuid4().hex
    posting = Posting(
        url=f"https://example.com/jobs/{suffix[:8]}",
        title="Data Engineer",
        description_raw=text,
        content_hash=suffix,
    )
    session.add(posting)
    await session.flush()
    return posting


async def _application(session, profile: Profile, posting: Posting) -> Application:
    application = Application(
        candidate_id=profile.candidate_id,
        profile_id=profile.id,
        posting_id=posting.id,
        url=posting.url,
        ats="greenhouse",
    )
    session.add(application)
    await session.flush()
    return application


async def _kept(session, profile: Profile, posting: Posting, *, score: float = 0.5) -> Match:
    match = Match(profile_id=profile.id, posting_id=posting.id, score=score, decision="interested")
    session.add(match)
    await session.flush()
    return match


async def _prepared(session, profile: Profile, match: Match, *, mode: str | None) -> Resume:
    """A résumé the batch left on the match, with a file behind it."""
    key = f"resumes/{profile.candidate_id}/prepared-{uuid.uuid4().hex[:6]}.pdf"
    get_storage().put(key, b"%PDF-1.4 prepared")
    stored = RESUME.model_dump(mode="json")
    if mode is not None:
        stored[cache.MODE_STAMP] = mode
    row = Resume(
        candidate_id=profile.candidate_id,
        version=2,
        storage_ref=key,
        parsed_json=stored,
        # Deliberately not the key apply would compute for today's provider.
        tailored_key="written-by-another-provider",
        tailored_by="ollama:night-model",
    )
    session.add(row)
    await session.flush()
    match.tailored_resume_id = row.id
    await session.flush()
    return row


@pytest.fixture
def targeted(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "tailor_mode", "targeted")


@pytest.fixture
def no_projects(monkeypatch) -> None:
    async def none(*args, **kwargs):  # noqa: ANN002, ANN003
        return []

    monkeypatch.setattr(apply_job, "_projects_for", none)


def _routed(monkeypatch, provider) -> list[int]:  # noqa: ANN001
    """Make `provider` the one tailoring is routed to, and count how often it is built."""
    built: list[int] = []

    def build():  # noqa: ANN202
        built.append(1)
        return provider

    monkeypatch.setattr(apply_job.llm_router, "tailor_resume", build)
    monkeypatch.setattr(compare_mod, "tailor_resume", build)
    return built


def _refuse_the_pdf(monkeypatch) -> None:
    """The PDF check finds a figure missing, whatever the file says."""
    monkeypatch.setattr(
        dispatch,
        "check_pdf",
        lambda *args, **kwargs: PdfCheck(
            extracted_chars=900,
            garbled=False,
            missing_terms=(),
            missing_figures=("1,000",),
            missing_technologies=("pytest",),
            parse=0.9,
            parse_findings=(),
            pages=1,
            layout_findings=(),
        ),
    )


async def _rows(session, profile: Profile) -> list[Resume]:
    from sqlalchemy import select

    found = await session.scalars(
        select(Resume)
        .where(Resume.candidate_id == profile.candidate_id, Resume.version > 1)
        .order_by(Resume.version)
    )
    return list(found.all())


# --------------------------------------------------------------------------
# The setting
# --------------------------------------------------------------------------


async def test_the_default_is_the_ordinary_rewrite() -> None:
    assert Settings().tailor_mode is TailorMode.REWRITE
    assert dispatch.active_mode() is TailorMode.REWRITE


async def test_a_misspelt_mode_is_refused_and_not_read_as_the_default() -> None:
    with pytest.raises(ValidationError):
        Settings(tailor_mode="targetted")
    with pytest.raises(ValueError, match="targetted"):
        dispatch.active_mode("targetted")


# --------------------------------------------------------------------------
# The cache
# --------------------------------------------------------------------------


async def test_the_ordinary_key_is_the_one_cached_rows_were_written_under() -> None:
    """Built here the way it was before there was a second mode. If this
    changes, every résumé already cached is a miss and is tailored again."""
    import hashlib

    source = uuid.UUID(int=7)
    before = hashlib.sha256(
        "\x1f".join(
            [str(source), "hash-1", TAILOR_SYSTEM.digest, TAILORING_REVISION, "gemini", "m-1", ""]
        ).encode("utf-8")
    ).hexdigest()

    now = tailoring_key(
        source_resume_id=source,
        content_hash="hash-1",
        projects=None,
        provider="gemini",
        model="m-1",
    )

    assert cache._SEP == "\x1f"
    assert now == before


async def test_a_row_with_no_stamp_was_a_rewrite() -> None:
    assert mode_of(Resume(parsed_json={"raw_lines": []})) is TailorMode.REWRITE
    assert mode_of(Resume(parsed_json=None)) is TailorMode.REWRITE
    assert mode_of(Resume(parsed_json={cache.MODE_STAMP: "targeted"})) is TailorMode.TARGETED


# --------------------------------------------------------------------------
# Apply, default mode: as it was
# --------------------------------------------------------------------------


async def test_a_prepared_resume_is_reused_before_any_provider_is_built(
    db_session, monkeypatch, no_projects
) -> None:
    """Whatever provider wrote it overnight. Re-tailoring would spend a second
    set of calls on a different document from the one already prepared."""
    profile, _ = await _owner(db_session)
    posting = await _posting(db_session)
    application = await _application(db_session, profile, posting)
    prepared = await _prepared(
        db_session, profile, await _kept(db_session, profile, posting), mode=None
    )

    def no_provider():  # noqa: ANN202
        raise AssertionError("a provider was built for a résumé that was already prepared")

    monkeypatch.setattr(apply_job.llm_router, "tailor_resume", no_provider)

    payload = await apply_job._tailor(db_session, application, profile, posting)

    assert application.tailored_resume_id == prepared.id
    assert payload == {"reused": True, "answered_by": "ollama:night-model"}


async def test_a_resume_prepared_under_the_other_mode_is_not_reused(
    db_session, monkeypatch, no_projects
) -> None:
    profile, _ = await _owner(db_session)
    posting = await _posting(db_session)
    application = await _application(db_session, profile, posting)
    prepared = await _prepared(
        db_session, profile, await _kept(db_session, profile, posting), mode="targeted"
    )
    built = _routed(monkeypatch, Scripted(LOADER))

    payload = await apply_job._tailor(db_session, application, profile, posting)

    assert built == [1]
    assert payload is not None and payload["mode"] == "rewrite"
    assert application.tailored_resume_id != prepared.id


# --------------------------------------------------------------------------
# Apply, targeted
# --------------------------------------------------------------------------


async def test_a_kept_revision_is_attached_and_its_pdf_was_read(
    db_session, monkeypatch, targeted, no_projects
) -> None:
    profile, _ = await _owner(db_session)
    posting = await _posting(db_session)
    application = await _application(db_session, profile, posting)
    provider = Scripted(ACCEPTED)
    _routed(monkeypatch, provider)

    payload = await apply_job._tailor(db_session, application, profile, posting)

    assert payload is not None
    assert payload["mode"] == "targeted" and payload["gained"] == [GAINED]
    assert payload["attached"] is True and payload["fallback_reason"] is None
    assert payload["pdf_check"]["ok"] is True
    assert payload["answered_by"] == "stub"
    assert len(provider.calls) == 1, "one bullet had something to gain"

    (row,) = await _rows(db_session, profile)
    assert application.tailored_resume_id == row.id
    assert mode_of(row) is TailorMode.TARGETED and row.tailored_key is not None
    assert ACCEPTED in tailorable_bullets(RESUME.model_validate(row.parsed_json))[1]


async def test_a_refused_pdf_is_not_attached_and_not_served_again(
    db_session, monkeypatch, targeted, no_projects
) -> None:
    """The base goes to the employer. And the second run asks the model again:
    it is not handed the refused document out of the cache."""
    profile, _ = await _owner(db_session)
    posting = await _posting(db_session)
    application = await _application(db_session, profile, posting)
    provider = Scripted(ACCEPTED)
    _routed(monkeypatch, provider)

    with monkeypatch.context() as broken:
        _refuse_the_pdf(broken)
        payload = await apply_job._tailor(db_session, application, profile, posting)

    assert payload is not None
    assert application.tailored_resume_id is None
    assert payload["attached"] is False and payload["pdf_check"]["ok"] is False
    assert "PDF verification failed" in payload["fallback_reason"]
    assert "base résumé used" in payload["fallback_reason"]
    (refused,) = await _rows(db_session, profile)
    assert refused.tailored_key is None

    again = await apply_job._tailor(db_session, application, profile, posting)

    assert again is not None and again.get("reused") is not True
    assert len(provider.calls) == 2
    assert application.tailored_resume_id not in (None, refused.id)


async def test_what_a_refused_pdf_lacked_is_not_logged(
    db_session, monkeypatch, targeted, no_projects
) -> None:
    """A figure and a technology off the résumé (§10). The log has how many."""
    profile, _ = await _owner(db_session)
    posting = await _posting(db_session)
    application = await _application(db_session, profile, posting)
    _routed(monkeypatch, Scripted(ACCEPTED))
    _refuse_the_pdf(monkeypatch)

    with structlog.testing.capture_logs() as logged:
        payload = await apply_job._tailor(db_session, application, profile, posting)

    (event,) = [entry for entry in logged if entry["event"] == "tailored_pdf_check_failed"]
    assert event["missing_figures"] == 1 and event["missing_technologies"] == 1
    assert "1,000" not in repr(logged) and "pytest" not in repr(logged)
    # The owner's own screen does name them.
    assert payload is not None and payload["pdf_check"]["missing_figures"] == ["1,000"]


async def test_nothing_to_gain_attaches_nothing_and_builds_no_provider(
    db_session, monkeypatch, targeted, no_projects
) -> None:
    profile, _ = await _owner(db_session)
    posting = await _posting(db_session, NOTHING_TO_GAIN)
    application = await _application(db_session, profile, posting)
    built = _routed(monkeypatch, Scripted(ACCEPTED))

    payload = await apply_job._tailor(db_session, application, profile, posting)

    assert built == []
    assert application.tailored_resume_id is None and await _rows(db_session, profile) == []
    assert payload is not None and payload["message"] == NO_GAIN
    assert payload["attached"] is False and payload["answered_by"] is None
    # The review screen prints this as a diff. It was a number once.
    assert payload["unified"] == ""


async def test_a_document_no_model_wrote_is_signed_by_none(
    db_session, monkeypatch, targeted
) -> None:
    """Nothing to gain, and a project to add. The document is published, and
    it is the base with a Projects section: no bullet was sent anywhere."""
    profile, _ = await _owner(db_session)
    posting = await _posting(db_session, NOTHING_TO_GAIN)
    application = await _application(db_session, profile, posting)
    project = Project(
        candidate_id=profile.candidate_id,
        source="github",
        external_id=uuid.uuid4().hex,
        name="ledger-checker",
        url="https://github.com/owner/ledger-checker",
        description="Checks ledger exports for missing rows",
        language="Python",
        topics_json=["pandas"],
    )
    db_session.add(project)
    await db_session.flush()

    async def one_project(*args, **kwargs):  # noqa: ANN002, ANN003
        return [project]

    monkeypatch.setattr(apply_job, "_projects_for", one_project)
    provider = Scripted(ACCEPTED)
    _routed(monkeypatch, provider)

    payload = await apply_job._tailor(db_session, application, profile, posting)

    assert provider.calls == []
    assert payload is not None and payload["answered_by"] is None
    (row,) = await _rows(db_session, profile)
    assert row.tailored_by is None
    assert application.tailored_resume_id == row.id


async def test_a_prepared_rewrite_is_not_what_a_targeted_run_sends(
    db_session, monkeypatch, targeted, no_projects
) -> None:
    profile, _ = await _owner(db_session)
    posting = await _posting(db_session)
    application = await _application(db_session, profile, posting)
    prepared = await _prepared(
        db_session, profile, await _kept(db_session, profile, posting), mode=None
    )
    _routed(monkeypatch, Scripted(ACCEPTED))

    payload = await apply_job._tailor(db_session, application, profile, posting)

    assert payload is not None and payload["mode"] == "targeted"
    assert application.tailored_resume_id != prepared.id


# --------------------------------------------------------------------------
# The nightly batch, targeted
# --------------------------------------------------------------------------


async def test_the_batch_attaches_a_kept_revision_for_one_call(db_session, targeted) -> None:
    profile, _ = await _owner(db_session)
    match = await _kept(db_session, profile, await _posting(db_session))
    provider = Scripted(ACCEPTED)

    result = await batch.run(db_session, provider, profile_id=str(profile.id))

    assert (result.tailored, result.failed, result.calls_spent) == (1, 0, 1)
    assert match.tailored_resume_id is not None


async def test_the_batch_does_not_keep_a_refused_pdf_for_the_next_night(
    db_session, monkeypatch, targeted
) -> None:
    profile, _ = await _owner(db_session)
    match = await _kept(db_session, profile, await _posting(db_session))
    provider = Scripted(ACCEPTED)

    with monkeypatch.context() as broken:
        _refuse_the_pdf(broken)
        first = await batch.run(db_session, provider, profile_id=str(profile.id))

    assert (first.tailored, first.failed) == (0, 1)
    assert match.tailored_resume_id is None
    (refused,) = await _rows(db_session, profile)
    assert refused.tailored_key is None

    second = await batch.run(db_session, provider, profile_id=str(profile.id))

    assert (second.tailored, second.reused) == (1, 0)
    assert match.tailored_resume_id not in (None, refused.id)


async def test_postings_with_nothing_to_gain_do_not_use_up_the_ceiling(
    db_session, targeted
) -> None:
    """They stay pending, so they are there every night, ahead of everything
    scored below them. The ceiling is on work, and reading them is none."""
    profile, _ = await _owner(db_session)
    for score in (0.9, 0.8):
        await _kept(db_session, profile, await _posting(db_session, NOTHING_TO_GAIN), score=score)
    reachable = await _kept(db_session, profile, await _posting(db_session), score=0.1)
    provider = Scripted(ACCEPTED)

    result = await batch.run(db_session, provider, profile_id=str(profile.id), limit=1)

    assert reachable.tailored_resume_id is not None
    assert (result.tailored, result.failed, result.calls_spent) == (1, 0, 1)
    assert [why for _, why in result.per_posting][:2] == [NO_GAIN, NO_GAIN]


async def test_the_ceiling_still_holds_for_postings_with_work(db_session, targeted) -> None:
    profile, _ = await _owner(db_session)
    first = await _kept(db_session, profile, await _posting(db_session), score=0.9)
    second = await _kept(db_session, profile, await _posting(db_session), score=0.1)

    await batch.run(db_session, Scripted(ACCEPTED), profile_id=str(profile.id), limit=1)

    assert first.tailored_resume_id is not None and second.tailored_resume_id is None


# --------------------------------------------------------------------------
# Compare models, the targeted column
# --------------------------------------------------------------------------


class FellBack(Scripted):
    """A routed provider whose primary was out: the local model answered.

    `FallbackProvider` says so on the instance that made the call, after it.
    """

    name = "gemini"

    async def complete(self, system, user, *, max_tokens=1024, temperature=0.2) -> str:  # noqa: ANN001
        answer = await super().complete(
            system, user, max_tokens=max_tokens, temperature=temperature
        )
        self.answered_by = "ollama:local-model"
        return answer


async def _column(session, profile: Profile, base: Resume, posting: Posting):  # noqa: ANN202
    _, bullets = tailorable_bullets(RESUME)
    return await compare_mod.tailor_with(
        session,
        provider_name="targeted",
        resume=base,
        parsed=RESUME,
        bullets=bullets,
        posting_text=posting.description_raw or "",
        projects=[],
        content_hash=posting.content_hash,
        posting_id=posting.id,
        mode="targeted",
    )


async def test_the_column_names_the_model_that_answered(db_session, monkeypatch) -> None:
    """Routed to a new instance each time it is asked for, as the router does.
    Built twice, the column read the instance that had made no call."""
    profile, base = await _owner(db_session)
    posting = await _posting(db_session)
    built: list[FellBack] = []

    def route() -> FellBack:
        built.append(FellBack(ACCEPTED))
        return built[-1]

    monkeypatch.setattr(compare_mod, "tailor_resume", route)

    column = await _column(db_session, profile, base, posting)

    assert len(built) == 1
    assert column.answered_by == "ollama:local-model"
    assert column.mode == "targeted" and column.gained == [GAINED]
    assert column.resume_id is not None and column.error is None
    assert column.pdf_check is not None and column.pdf_check["ok"] is True
    stored = await db_session.get(Resume, column.resume_id)
    assert stored is not None and stored.tailored_by == "ollama:local-model"


async def test_the_column_with_nothing_to_gain_sends_nothing(db_session, monkeypatch) -> None:
    profile, base = await _owner(db_session)
    posting = await _posting(db_session, NOTHING_TO_GAIN)
    built = _routed(monkeypatch, Scripted(ACCEPTED))

    column = await _column(db_session, profile, base, posting)

    assert built == []
    assert column.resume_id is None and column.answered_by is None
    assert column.error is not None and NO_GAIN in column.error
    assert await _rows(db_session, profile) == []


async def test_a_column_whose_pdf_was_refused_cannot_be_chosen_now_or_later(
    db_session, monkeypatch
) -> None:
    """No résumé id, so the select route has nothing to accept. And comparing
    again does not bring the refused document back as "reused"."""
    profile, base = await _owner(db_session)
    posting = await _posting(db_session)
    provider = Scripted(ACCEPTED)
    _routed(monkeypatch, provider)

    with monkeypatch.context() as broken:
        _refuse_the_pdf(broken)
        refused = await _column(db_session, profile, base, posting)

    assert refused.resume_id is None
    assert refused.error is not None and "not offered" in refused.error
    assert refused.pdf_check is not None and refused.pdf_check["ok"] is False
    key = tailoring_key(
        source_resume_id=base.id,
        content_hash=posting.content_hash,
        projects=[],
        provider="stub",
        model=getattr(provider, "model", None),
        mode="targeted",
    )
    assert await find_cached(db_session, candidate_id=profile.candidate_id, key=key) is None

    again = await _column(db_session, profile, base, posting)

    assert again.reused is False and again.resume_id is not None
    assert len(provider.calls) == 2
