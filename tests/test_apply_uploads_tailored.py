"""Which résumé file the employer actually receives.

Phase 3 built the rewriter, the guard, the diff and the PDF, and the apply
pipeline uploaded the base résumé anyway: `_resume_path` read
`profile.base_resume_id`, and `adapter.fill` ran *before* `_tailor`. Every
application sent the untailored document while the review screen showed a diff
of a file nobody had sent, and the comment above the call claimed the opposite.

Nothing caught it because no test asserted which path reaches the file input.
These do.
"""

from __future__ import annotations

import uuid

import pytest

from apps.worker import apply_job
from packages.core.models import Application, Candidate, Profile, Resume, User
from packages.core.storage import get_storage, resume_key


async def _setup(db_session, *, with_tailored: bool) -> tuple[Application, Profile]:
    suffix = uuid.uuid4().hex[:8]
    user = User(email=f"u-{suffix}@example.com")
    db_session.add(user)
    await db_session.flush()
    candidate = Candidate(user_id=user.id, name="Owner", email=f"u-{suffix}@example.com")
    db_session.add(candidate)
    await db_session.flush()

    storage = get_storage()
    base_key = resume_key(str(candidate.id), 1, "resume.pdf")
    storage.put(base_key, b"%PDF-1.4 base")
    base = Resume(candidate_id=candidate.id, version=1, storage_ref=base_key, parsed_json={})
    db_session.add(base)
    await db_session.flush()

    profile = Profile(candidate_id=candidate.id, label="default", base_resume_id=base.id)
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

    if with_tailored:
        tailored_key_path = resume_key(str(candidate.id), 2, "tailored.pdf")
        storage.put(tailored_key_path, b"%PDF-1.4 tailored")
        tailored = Resume(
            candidate_id=candidate.id, version=2, storage_ref=tailored_key_path, parsed_json={}
        )
        db_session.add(tailored)
        await db_session.flush()
        application.tailored_resume_id = tailored.id
        await db_session.flush()

    return application, profile


async def test_the_tailored_resume_is_the_file_that_gets_uploaded(db_session) -> None:
    """The bug this file exists for."""
    application, profile = await _setup(db_session, with_tailored=True)

    path = await apply_job._resume_path(db_session, application, profile)

    assert path is not None
    assert path.endswith("tailored.pdf"), f"uploaded the wrong document: {path}"


async def test_the_base_resume_is_used_when_nothing_was_tailored(db_session) -> None:
    """Tailoring is allowed to fail; sending no résumé at all is not the answer."""
    application, profile = await _setup(db_session, with_tailored=False)

    path = await apply_job._resume_path(db_session, application, profile)

    assert path is not None
    assert path.endswith("resume.pdf")


async def test_a_missing_tailored_file_falls_back_rather_than_sending_nothing(
    db_session, caplog
) -> None:
    """A vanished PDF must not turn into an application with no résumé.

    The fallback is logged: silently sending the base résumé while the
    application claims a tailored one is how this went unnoticed the first
    time.
    """
    application, profile = await _setup(db_session, with_tailored=True)
    tailored = await db_session.get(Resume, application.tailored_resume_id)
    get_storage().path_for(tailored.storage_ref).unlink()

    path = await apply_job._resume_path(db_session, application, profile)

    assert path is not None
    assert path.endswith("resume.pdf")


async def test_tailoring_runs_before_the_form_is_filled() -> None:
    """Ordering is the whole defect — assert it in the source, not by luck.

    A unit test cannot easily drive the browser pipeline, so this reads the
    function: `_tailor` must appear before `adapter.fill`, or the fill uploads
    a résumé that has not been written yet.
    """
    import inspect

    source = inspect.getsource(apply_job._run_pipeline)
    tailor_at = source.index("_tailor(")
    fill_at = source.index("adapter.fill(")

    assert tailor_at < fill_at, "adapter.fill runs before _tailor; the base résumé gets uploaded"


@pytest.mark.parametrize("field", ["content_hash"])
def test_parsed_posting_has_no_content_hash(field: str) -> None:
    """Why `tailoring_key` takes the hash rather than a "posting".

    Two things are called `posting` here: the `Posting` row, which has this
    field, and `ParsedPosting`, which the adapter reads off the page and which
    does not. A parameter typed for one accepts the other at runtime.
    """
    from packages.ats.base import ParsedPosting

    assert field not in ParsedPosting.model_fields
