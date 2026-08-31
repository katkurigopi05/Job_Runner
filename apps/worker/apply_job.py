"""The apply pipeline.

parse_posting → enumerate_fields → fill → screenshot → (approval) → submit.

The shape of the run is fixed by two rules:

- Nothing submits without approval unless AUTO_SUBMIT is on *and* the profile
  opts in above its match threshold (CLAUDE.md §2.3).
- Any required question the agent cannot answer parks the application at
  `needs_review` carrying the question's exact text (CLAUDE.md §2.4).
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.worker.browser import browser_page
from packages.ats.answers import asks_for_cover_letter, build_answers
from packages.ats.base import (
    FillReport,
    ManualCompletionRequired,
    Question,
    SiteError,
    UnsupportedSiteError,
)
from packages.ats.registry import adapter_for
from packages.ats.screen import ScreenReport, screen
from packages.core.config import get_settings
from packages.core.enums import ApplicationStatus, FailureReason
from packages.core.models import (
    Application,
    Candidate,
    Match,
    Posting,
    Profile,
    Project,
    Resume,
)
from packages.core.queue import ClaimedTask
from packages.core.state import WorkClaim, begin_work, transition
from packages.core.storage import get_storage, receipt_key
from packages.github.select import relevant_for_posting
from packages.llm import router as llm_router
from packages.matching.embed import get_embedder
from packages.matching.pick_resume import choose_base_resume
from packages.tailor.bullets import tailorable_bullets
from packages.tailor.cache import find_cached, tailoring_key

log = structlog.get_logger(__name__)


async def _match_score(session: AsyncSession, application: Application) -> float | None:
    """This profile's score for the posting, or None if it was never scored.

    An application created straight from a URL has no posting attached, so
    there is nothing to have scored. Callers must treat None as "did not clear
    the threshold" rather than substituting a default.
    """
    if application.posting_id is None:
        return None
    return await session.scalar(
        select(Match.score).where(
            Match.profile_id == application.profile_id,
            Match.posting_id == application.posting_id,
        )
    )


class TaskPayloadError(Exception):
    """The task payload does not name a workable application."""


async def handle_apply(session: AsyncSession, claimed: ClaimedTask) -> None:
    """Run one apply task. Caller holds the lease and owns the transaction.

    Idempotent: safe to run again after a crash at any point.
    """
    raw_id = claimed.task.payload_json.get("application_id")
    if not raw_id:
        raise TaskPayloadError("payload has no application_id")

    application = await session.get(Application, raw_id)
    if application is None:
        raise TaskPayloadError(f"application {raw_id} does not exist")

    if claimed.reclaimed:
        log.info(
            "reclaimed_expired_lease",
            application_id=str(application.id),
            previous_owner=claimed.previous_owner,
            own_lease=claimed.reclaimed_from_self,
            attempts=claimed.task.attempts,
        )

    claim = await begin_work(session, application)

    if claim is WorkClaim.ALREADY_DONE:
        log.info(
            "application_already_terminal",
            application_id=str(application.id),
            status=application.status,
        )
        return

    if claim is WorkClaim.RESUMED:
        log.info("resuming_abandoned_run", application_id=str(application.id))

    candidate = await session.get(Candidate, application.candidate_id)
    profile = await session.get(Profile, application.profile_id)
    if candidate is None or profile is None:  # pragma: no cover - FK guarantees
        raise TaskPayloadError("application is missing its candidate or profile")

    try:
        await _run_pipeline(session, application, candidate, profile)
    except UnsupportedSiteError as exc:
        await _fail(session, application, FailureReason.UNSUPPORTED_SITE, str(exc))
    except ManualCompletionRequired as exc:
        # A blocked site is a scope boundary, not a bug to work around.
        await _fail(session, application, FailureReason.MANUAL_COMPLETION_REQUIRED, str(exc))
    except SiteError as exc:
        await _fail(session, application, FailureReason.SITE_ERROR, str(exc))


async def _run_pipeline(
    session: AsyncSession,
    application: Application,
    candidate: Candidate,
    profile: Profile,
) -> None:
    adapter = adapter_for(application.url)
    application.ats = adapter.name

    async with browser_page(adapter.name) as page:
        response = await page.goto(application.url, wait_until="domcontentloaded")

        # A withdrawn posting is `job_closed`, not `site_error`.
        #
        # The adapters read "closed" from on-page text — "this posting is
        # closed", "position has been filled" — which a 404 page does not
        # carry. So a job taken down entirely got as far as `enumerate_fields`,
        # found no form, and was recorded as `site_error`: a code that says
        # *our side is broken* and invites a retry, for the one outcome that is
        # both expected and permanent.
        #
        # Checked here rather than per adapter because the HTTP status is the
        # authoritative signal and is the same for every ATS. 410 says removed;
        # 404 on a board URL that we hold a record of means the same thing. 403
        # is deliberately not included — that is usually automation being
        # blocked, which §2.5 makes `manual_completion_required`.
        if response is not None and response.status in (404, 410):
            await _fail(
                session,
                application,
                FailureReason.JOB_CLOSED,
                f"posting returned HTTP {response.status} — it has been taken down",
            )
            return

        posting = await adapter.parse_posting(page)
        if posting.closed:
            await _fail(session, application, FailureReason.JOB_CLOSED, "posting is closed")
            return

        questions = await adapter.enumerate_fields(page)

        # Read the form before answering any of it. A question whose honest
        # answer ends the application, or one an employer should not be
        # asking, is worth surfacing while the decision is still the owner's
        # — not after a fill, a screenshot and a rejection.
        screening = screen(questions, profile)

        # Answers the owner supplied at review, if this is a resumed run.
        review = application.review_json or {}
        owner_answers = review.get("owner_answers") or {}

        # §9 Phase 3 — before the fill, not after it. This ran after
        # `adapter.fill` for as long as it existed, which meant the tailored
        # PDF was written, linked to the application and rendered into the
        # review diff *after* the form had already been handed the base
        # résumé. Every application uploaded the untailored document while the
        # comment here claimed the opposite and the review screen showed a diff
        # of a file nobody sent. Tailoring has to finish before there is
        # anything worth uploading.
        diff = await _tailor(session, application, profile, posting)

        # Same rule as the résumé above, for the same reason: the document has
        # to exist before there is anything to put in the field. Reads
        # `questions` because most forms never ask for a letter, and writing
        # one for a form with nowhere to put it is a provider call spent on a
        # document no employer sees.
        letter = await _cover_letter(session, application, profile, posting, questions)

        answers = build_answers(
            questions,
            candidate,
            profile,
            extra=owner_answers,
            resume_path=await _resume_path(session, application, profile),
            cover_letter_text=(letter or {}).get("text"),
            cover_letter_path=_letter_path(application),
        )
        report = await adapter.fill(page, answers)

        report.screenshot_ref = await _capture(page, application, "filled-form.png")

        await _decide(
            session,
            application,
            profile,
            report,
            adapter,
            page,
            diff,
            screening=screening,
            cover_letter=letter,
        )


async def _posting_hash(session: AsyncSession, application: Application) -> str | None:
    """The stored posting's content hash, when the application names one.

    None for an application created straight from a URL, which never had a
    `Posting` row. That is uncacheable rather than an error — see
    `packages/tailor/cache.py` on why a missing hash must not be replaced by a
    weaker identifier.
    """
    if application.posting_id is None:
        return None
    posting = await session.get(Posting, application.posting_id)
    return posting.content_hash if posting is not None else None


async def _resume_path(
    session: AsyncSession, application: Application, profile: Profile
) -> str | None:
    """Absolute path of the résumé to upload — the tailored one when there is one.

    The tailored document is the point of Phase 3. Uploading the base résumé
    instead means the guard ran, the projects were selected, the diff was
    rendered for review, and the employer received none of it.

    Falls back to the base résumé rather than to nothing. Tailoring is allowed
    to fail — `_tailor` returns None on a render failure or a refused rewrite,
    and §2.1 would rather send an honest untailored résumé than no résumé at
    all. What it must not do is fail silently, so the fallback is logged.
    """
    candidates: list[tuple[str, uuid.UUID]] = []
    if application.tailored_resume_id is not None:
        candidates.append(("tailored", application.tailored_resume_id))
    if profile.base_resume_id is not None:
        candidates.append(("base", profile.base_resume_id))

    for kind, resume_id in candidates:
        resume = await session.get(Resume, resume_id)
        if resume is None:
            continue
        path = get_storage().path_for(resume.storage_ref)
        if not path.is_file():
            log.warning("resume_file_missing", kind=kind, storage_ref=resume.storage_ref)
            continue
        if kind == "base" and application.tailored_resume_id is not None:
            log.warning("uploading_base_resume_tailored_file_unusable")
        return str(path)

    return None


async def _prepared_resume(
    session: AsyncSession, application: Application, profile: Profile
) -> Any | None:
    """The résumé a batch already tailored for this posting, if there is one.

    Returns None rather than raising when the stored file has gone missing —
    the run then tailors normally, which is slower and correct.
    """
    if application.posting_id is None:
        return None

    match = await session.scalar(
        select(Match).where(
            Match.profile_id == profile.id,
            Match.posting_id == application.posting_id,
            Match.tailored_resume_id.is_not(None),
        )
    )
    if match is None or match.tailored_resume_id is None:
        return None

    resume = await session.get(Resume, match.tailored_resume_id)
    if resume is None:
        return None
    if not get_storage().path_for(resume.storage_ref).is_file():
        log.warning("prepared_resume_missing", storage_ref=resume.storage_ref)
        return None
    return resume.id


async def _projects_for(
    session: AsyncSession, candidate_id: uuid.UUID, posting_text: str
) -> list[Project]:
    """GitHub projects that evidence this posting, for the résumé being sent.

    The scorer has counted these all along — `matching.score.profile_text` is
    résumé *plus* projects, which is why a posting whose skills live in the
    owner's repositories still ranks and is never filtered out. The document
    did not agree: this call passed no projects, so the PDF the employer
    received omitted the very evidence that surfaced the job.
    """
    inventory = list(
        (await session.scalars(select(Project).where(Project.candidate_id == candidate_id))).all()
    )
    # The embedder is what lets a repository count when it describes the same
    # work in different words. On the lexical default it cannot — that backend
    # measures vocabulary overlap, so the second pass finds nothing and this
    # degrades to shared-vocabulary matching. Set EMBEDDING_BACKEND to
    # sentence-transformers for it to do anything.
    return relevant_for_posting(inventory, posting_text, embedder=get_embedder())


def _answered_by(provider: Any | None) -> str | None:
    """Which model actually answered — read *after* the call, never before.

    `FallbackProvider` resets `answered_by` to the primary at the top of every
    call and rewrites it only once the primary has failed, so a value read
    ahead of time names the model that did not answer. That is precisely the
    case worth surfacing: a document produced by llama3.1 after the remote
    allowance ran out is not the document Gemini would have produced, and the
    owner approving it should be able to tell.

    None when no provider was ever built, which is honest — an unrecorded model
    is not a guessed one.
    """
    if provider is None:
        return None
    return getattr(provider, "answered_by", None) or getattr(provider, "name", None)


async def _base_resume_id(
    session: AsyncSession,
    application: Application,
    profile: Profile,
    posting: Any,
) -> Any | None:
    """Which of the owner's résumés this application starts from.

    Every path used to read `profile.base_resume_id` and nothing else, so a
    candidate with a backend résumé, a data one and an ML one had two of them
    unreachable. Nothing failed and nothing was logged — the tailorer did its
    whole job on the wrong document, and the employer got a competent ML
    résumé for a backend role.

    **The choice is recorded and then reused**, which is the half that is easy
    to leave out. Approving a parked application re-enters the pipeline from
    the top, so re-deciding here would let an upload between the two runs
    silently swap the document the owner reviewed — the same defect as
    `resume_pinned`, one screen earlier.
    """
    recorded = (application.review_json or {}).get("base_resume")
    if isinstance(recorded, dict) and recorded.get("resume_id"):
        return uuid.UUID(str(recorded["resume_id"]))

    # `posting` is a parsed page here, not a stored row — the parameter is
    # `Any` for that reason — so the text is assembled where that is known
    # rather than inside the selector.
    haystack = f"{getattr(posting, 'title', '') or ''}\n{posting.description_raw or ''}"
    choice = await choose_base_resume(session, profile, haystack)
    if choice is None:
        return profile.base_resume_id

    application.review_json = {
        **(application.review_json or {}),
        "base_resume": choice.as_dict(),
    }
    log.info(
        "base_resume_selected",
        version=choice.version,
        reason=choice.reason,
        considered=len(choice.considered),
    )
    return choice.resume_id


async def _tailor(
    session: AsyncSession,
    application: Application,
    profile: Profile,
    posting: Any,
) -> dict[str, Any] | None:
    """Tailor the base résumé for this posting, render it, and return the diff.

    Phase 3 built the rewriter, the fabrication guard, and the diff, and
    nothing ever called them — tailored_resume_id was always null, so the
    review screen had nothing to show. This is the call that makes a diff
    exist.

    Rendering happens here, off the same `TailorResult` the diff is computed
    from, because those two must not be able to disagree. Tailoring twice
    would be non-deterministic on any real provider, and the owner would be
    reading one document and uploading another.

    §2.1 is enforced inside tailor_bullets: every rewrite is guard-checked
    against the source, and one that introduces an unsupported entity falls
    back to the original line. `rejected` counts those, and the review screen
    shows it — a guard that silently substituted would be indistinguishable
    from a guard that never fired.

    Returns None when there is nothing to tailor. A failure here must not stop
    the application: the untailored résumé is still a correct résumé.
    """
    # An owner's choice at review outranks anything this function would compute.
    #
    # Approving a parked application resumes the pipeline from the top, so this
    # runs a second time — and every path below assigns `tailored_resume_id`.
    # That silently discarded two decisions the owner had already made on the
    # review screen: a hand-edit of the attached résumé, and a pick from the
    # model comparison. The screen showed the chosen document, the employer
    # received the re-tailored one, and nothing anywhere reported the swap. It
    # is the §15 defect exactly — a review screen describing a file that is not
    # the file being uploaded.
    #
    # The pin is checked against `tailored_resume_id` rather than trusted on its
    # own: if the row no longer points where the pin says, something else moved
    # it and re-tailoring is the safer answer.
    pinned = (application.review_json or {}).get("resume_pinned") or {}
    pinned_id = pinned.get("resume_id")
    if pinned_id and str(application.tailored_resume_id) == str(pinned_id):
        row = await session.get(Resume, application.tailored_resume_id)
        log.info("tailoring_skipped_owner_pinned", source=pinned.get("source"))
        # The stored diff is kept rather than recomputed — it still describes
        # what tailoring did to the document this one came from, which is the
        # honest account. `owner_pinned` is what tells the review screen the
        # diff no longer describes the file being sent.
        stored = dict((application.review_json or {}).get("resume_diff") or {})
        stored["owner_pinned"] = pinned.get("source") or "owner_edit"
        stored["reused"] = True
        if row is not None and row.tailored_by:
            stored.setdefault("answered_by", row.tailored_by)
        return stored

    posting_text = posting.description_raw or ""
    base_id = await _base_resume_id(session, application, profile, posting)
    if base_id is None or not posting_text.strip():
        return None

    # A batch run may already have tailored this posting overnight
    # (packages/tailor/batch.py). Reusing it is the whole point: it takes the
    # slowest step out of the critical path, and re-tailoring would spend a
    # second set of provider calls to produce a *different* document from the
    # one the owner may already have reviewed.
    prepared = await _prepared_resume(session, application, profile)
    if prepared is not None:
        application.tailored_resume_id = prepared
        # No provider is built on this path, so the model cannot be read live —
        # it has to come off the row the batch wrote. A reused résumé that
        # cannot name its model is the case this whole field exists for.
        prepared_row = await session.get(Resume, prepared)
        return {
            "reused": True,
            "answered_by": prepared_row.tailored_by if prepared_row else None,
        }

    resume = await session.get(Resume, base_id)
    if resume is None or not resume.parsed_json:
        return None

    try:
        from packages.tailor.diff import summarize
        from packages.tailor.guard import SourceCorpus
        from packages.tailor.parse import ParsedResume
        from packages.tailor.rewrite import tailor_bullets

        parsed = ParsedResume.model_validate(resume.parsed_json)
        _, bullets = tailorable_bullets(parsed)
        if not bullets:
            return None

        # from_resume, not from_texts: the guard has to be able to tell one
        # employer's entry from another's, or a rewrite can move a metric
        # between them and still trace to "the résumé".
        corpus = SourceCorpus.from_resume(parsed)
        provider = llm_router.tailor_resume()

        # Chosen before the key rather than inside the publish call, because
        # which projects get attached changes the document and so has to be
        # part of what identifies it.
        projects = await _projects_for(session, resume.candidate_id, posting_text)
        # From the stored row, not from `posting`: the adapter hands this
        # function a `ParsedPosting`, which carries no content hash.
        cache_key = tailoring_key(
            source_resume_id=resume.id,
            content_hash=await _posting_hash(session, application),
            projects=projects,
            provider=getattr(provider, "name", "unknown"),
            model=getattr(provider, "model", None),
        )
        cached = await find_cached(session, candidate_id=resume.candidate_id, key=cache_key)
        if cached is not None:
            # Re-applying to a posting already tailored for sends nothing to a
            # provider. §2.8 asks that the upload be minimal as well as
            # audited, and the cheapest upload is the one not made.
            application.tailored_resume_id = cached.id
            log.info("tailored_resume_reused", resume_id=str(cached.id))
            return {
                "changed": 0,
                "unchanged": 0,
                "rejected": 0,
                "reused": True,
                # Same reasoning as the batch path above: the call that wrote
                # this document happened in another run, so the row is the only
                # source for which model answered it.
                "answered_by": cached.tailored_by,
            }

        result = await tailor_bullets(provider, bullets, posting_text, corpus)
        # After the rewrites, never before — see `_answered_by`.
        answered_by = _answered_by(provider)

        # Scored against the same posting, before and after, so the review
        # screen can say what tailoring bought rather than only what it
        # changed. `apply_rewrites` is called here anyway — `publish_tailored`
        # below does the same thing internally — and the duplicate is cheap
        # next to re-deriving it on the screen from two stored documents.
        from packages.tailor.publish import apply_rewrites

        summary = summarize(
            result,
            source=parsed,
            tailored=apply_rewrites(parsed, result, posting_text=posting_text),
            job_description=posting_text,
        )

        # The file the owner uploads. Rendered even when every rewrite was
        # rejected: that document is the source résumé with the current
        # Projects section rebuilt into it, which is still the right thing to
        # send and is not what sits in storage as the base.
        from packages.tailor.publish import publish_tailored

        published = await publish_tailored(
            session,
            candidate_id=resume.candidate_id,
            parsed=parsed,
            result=result,
            projects=projects,
            posting_text=posting_text,
            tailored_key=cache_key,
            answered_by=answered_by,
            # From the application, not from `posting`: the adapter's
            # ParsedPosting is read off the page and has no row id.
            posting_id=application.posting_id,
        )
        if published is not None:
            application.tailored_resume_id = published.id

        return {
            "changed": summary.changed,
            "unchanged": summary.unchanged,
            # Rewrites the guard refused. Surfaced, not swallowed.
            "rejected": summary.rejected,
            # And separately, bullets the model never answered at all. Folding
            # these into `rejected` would report a provider outage as the guard
            # doing its job.
            "provider_failures": summary.provider_failures,
            "answered_by": answered_by,
            # The ATS score before and after this run. Hand-copied like every
            # other field here, and that is exactly how it was missed the first
            # time: `summarize` computed it, `DiffSummary` carried it, and this
            # dict — which is what actually becomes `review_json["resume_diff"]`
            # — listed the fields by name and did not list this one. The screen
            # rendered nothing and reported no error, because there was none.
            # §15's defect in miniature, so `tests/test_apply_review_payload.py`
            # asserts the payload rather than the model.
            "ats": summary.ats.model_dump() if summary.ats else None,
            # Added second, to the same list, for the same reason — and this
            # comment is the whole point of the one above it. A field computed
            # by `summarize`, carried on `DiffSummary`, typed on the client and
            # rendered by a component still never arrives unless it is named
            # here.
            "recruiter": summary.recruiter.model_dump() if summary.recruiter else None,
            "unified": summary.unified,
            "changes": [change.model_dump() for change in summary.changes],
        }
    except Exception as exc:  # noqa: BLE001 - tailoring is an enhancement
        log.warning("tailoring_failed", error=type(exc).__name__)
        return None


def _stored_letter(application: Application) -> dict[str, Any] | None:
    """The letter this application already has, if it still has one.

    A resumed run — the owner approved at review and the pipeline replays —
    must send the letter that was approved, not a second one written from the
    same prompt. Any real provider returns different prose the second time, so
    without this the owner reviews one letter and the employer reads another.
    That is the tailored-résumé defect exactly, and the check is cheap.
    """
    previous = (application.review_json or {}).get("cover_letter") or {}
    if not previous.get("accepted") or not previous.get("text"):
        return None
    ref = application.cover_letter_ref
    if not ref or not get_storage().path_for(ref).is_file():
        return None
    return dict(previous)


async def _cover_letter(
    session: AsyncSession,
    application: Application,
    profile: Profile,
    posting: Any,
    questions: list[Question],
) -> dict[str, Any] | None:
    """Write the letter this posting asks for, vet it, store it.

    §9 Phase 3 lists a cover letter and `packages/tailor/cover.py` writes one:
    it sifts the sentences that do not trace to the résumé, vets the remainder
    through the fabrication guard, and returns nothing rather than falling
    back. Until now nothing called it, `Application.cover_letter_ref` was
    written by nobody, and no application carried a letter.

    Three refusals, in order of how much they cost:

    - **The form never asked.** Most do not. Writing a letter no field can
      hold is a provider call spent on a document that goes nowhere, so the
      questions are read first and the model is not reached at all.
    - **There is nothing to write from.** No base résumé, or a posting whose
      description never parsed — a letter about a job we did not read is
      exactly the invention §2.1 forbids.
    - **The guard refused it.** Returned with the reason, and no file. A
      letter has no original to fall back to, so the alternative to a bad one
      is none; recording *why* is what keeps that different from never trying.

    Returns the record the review screen shows, or None when no letter was
    called for. Never raises: a failure here leaves an application with a
    filled form and an empty optional field, which is a worse application and
    still an application.
    """
    if not any(asks_for_cover_letter(question) for question in questions):
        return None

    reused = _stored_letter(application)
    if reused is not None:
        log.info("cover_letter_reused", ref=application.cover_letter_ref)
        return {**reused, "reused": True}

    posting_text = posting.description_raw or ""
    base_id = await _base_resume_id(session, application, profile, posting)
    if base_id is None or not posting_text.strip():
        return None

    # The same document the résumé was tailored from. A letter written off a
    # different résumé than the one attached would contradict it.
    resume = await session.get(Resume, base_id)
    if resume is None or not resume.parsed_json:
        return None

    # Bound before the try so the failure path can still say which provider was
    # being asked, and say nothing when the failure was building it.
    provider: Any | None = None
    try:
        from packages.tailor.cover import write
        from packages.tailor.guard import SourceCorpus
        from packages.tailor.parse import ParsedResume
        from packages.tailor.publish import publish_cover_letter

        parsed = ParsedResume.model_validate(resume.parsed_json)
        # The base résumé, not the tailored one. Every fact in a tailored
        # résumé traces to the base by construction — the guard enforces it —
        # so the base corpus is the wider of the two nowhere and the safer of
        # the two here, and it does not depend on tailoring having succeeded.
        provider = llm_router.write_cover_letter()
        letter = await write(
            provider,
            resume_text=parsed.text,
            job_description=posting_text,
            corpus=SourceCorpus.from_resume(parsed),
            # The one thing outside the résumé the letter may name.
            company=posting.company,
        )
    except Exception as exc:  # noqa: BLE001 - a letter is an enhancement
        log.warning("cover_letter_failed", error=type(exc).__name__)
        return {
            "accepted": False,
            "rejected_reason": f"error: {type(exc).__name__}",
            "answered_by": _answered_by(provider),
        }

    outcome: dict[str, Any] = {
        "accepted": letter.usable,
        "rejected_reason": letter.rejected_reason,
        "word_count": letter.word_count,
        "entities_checked": letter.entities_checked,
        # A letter that survived only because most of it was deleted is worth
        # seeing on the review screen, not just in the accept/refuse bit.
        "sentences_dropped": letter.sentences_dropped,
        # §7's fallback applies to `write_cover_letter` exactly as it does to
        # tailoring, so a letter written by the local model after the allowance
        # ran out has to be distinguishable from one written by the cloud one.
        # The résumé records this on its own row; a letter has no row, so it
        # rides in the review record beside the text it describes.
        "answered_by": _answered_by(provider),
    }

    if not letter.usable:
        # Never the letter itself — §10 keeps résumé-derived text out of logs.
        log.info("cover_letter_refused", reason=letter.rejected_reason)
        return outcome

    ref = publish_cover_letter(letter.text, application_id=str(application.id))
    if ref is None:
        # Storage failed, so there is no file to attach. The text would still
        # fill a textarea, but then an approved letter would exist in one
        # shape and not the other depending on the employer's form — refuse
        # both rather than ship the inconsistency.
        return {**outcome, "accepted": False, "rejected_reason": "letter could not be stored"}

    application.cover_letter_ref = ref
    # Kept alongside the ref because the review screen has to show the letter
    # the owner is approving, and a PDF is not readable back as text.
    return {**outcome, "ref": ref, "text": letter.text}


def _letter_path(application: Application) -> str | None:
    """The stored letter as a path, for a field that wants a file upload."""
    ref = application.cover_letter_ref
    if not ref:
        return None
    path = get_storage().path_for(ref)
    if not path.is_file():
        log.warning("cover_letter_file_missing", storage_ref=ref)
        return None
    return str(path)


async def _capture(page: Any, application: Application, name: str) -> str | None:
    """Screenshot into storage. A failed capture must not fail the run.

    A full-page shot of a long posting can be very large, so an oversized one
    is retried at viewport size rather than discarded — a smaller receipt is
    worth more than none.
    """
    try:
        key = receipt_key(str(application.id), name)
        storage = get_storage()
        target = storage.path_for(key)
        target.parent.mkdir(parents=True, exist_ok=True)

        await page.screenshot(path=str(target), full_page=True)

        enforce = getattr(storage, "enforce_limit", None)
        if enforce is not None and not enforce(key):
            log.warning("screenshot_over_limit_retrying_viewport", key=key)
            await page.screenshot(path=str(target), full_page=False)
            if not enforce(key):
                log.warning("screenshot_still_over_limit_discarded", key=key)
                return None

        return key
    except Exception as exc:  # noqa: BLE001 - the audit artifact is best-effort
        log.warning("screenshot_failed", error=type(exc).__name__)
        return None


async def _decide(
    session: AsyncSession,
    application: Application,
    profile: Profile,
    report: FillReport,
    adapter: Any,
    page: Any,
    resume_diff: dict[str, Any] | None = None,
    screening: ScreenReport | None = None,
    cover_letter: dict[str, Any] | None = None,
) -> None:
    """Park for review, or submit — the approval gate.

    Three independent conditions must all hold before anything is sent:
    every required question is answered, AUTO_SUBMIT is on, and this profile
    opted in. Otherwise the run stops with the form filled and screenshotted.

    A knock-out finding stops the *unattended* path as a fourth condition —
    see below. It never overrides the owner's own approval.
    """
    settings = get_settings()

    application.review_json = {
        **(application.review_json or {}),
        "fill_rate": round(report.fill_rate, 3),
        "filled": [f.model_dump() for f in report.filled],
        "skipped": [s.model_dump() for s in report.skipped],
        # The exact question text, preserved for the owner.
        "unanswered": [q.model_dump() for q in report.unanswered],
        "screenshot_ref": report.screenshot_ref,
        "resume_diff": resume_diff,
        # Read off the form before anything was answered.
        "screening": screening.as_dict() if screening else None,
        # The letter, or the guard's reason there is not one. A refusal has to
        # be visible here: a form that asked for a letter and got none looks
        # identical to a form that never asked, and the owner is the one who
        # decides whether to write it by hand.
        "cover_letter": cover_letter,
    }

    if not report.is_complete:
        await transition(
            session,
            application,
            ApplicationStatus.NEEDS_REVIEW,
            payload={
                "reason": "required questions could not be answered",
                "questions": [q.question for q in report.unanswered if q.required],
            },
        )
        return

    # The owner already looked at this form and said yes. That approval *is*
    # the authorization CLAUDE.md §2.3 asks for, so it stands on its own —
    # AUTO_SUBMIT and the score threshold govern submitting *without* a human,
    # and re-applying them here would park an approved application forever.
    if (application.review_json or {}).get("owner_approved"):
        await _submit(session, application, adapter, page)
        return

    # A knock-out is a question whose honest answer from this profile likely
    # disqualifies. Sending anyway costs the owner nothing and a recruiter
    # their time, which is the spray behaviour this project exists not to do.
    # So it parks — but only on the unattended path. The owner having already
    # approved this form outranks it, and that branch is above.
    if screening is not None and screening.knock_outs:
        await transition(
            session,
            application,
            ApplicationStatus.NEEDS_REVIEW,
            payload={
                "reason": "the form asks a question this profile likely fails",
                "questions": [q.label for q in screening.knock_outs],
            },
        )
        return

    if not (settings.auto_submit and profile.auto_submit):
        await transition(
            session,
            application,
            ApplicationStatus.NEEDS_REVIEW,
            payload={"reason": "awaiting owner approval before submit"},
        )
        return

    # §2.3 makes the threshold part of the opt-in, not advice: auto-submit is
    # permitted "above a match-score threshold you choose". A profile carrying
    # min_match_score=0.9 must not submit to something scored 0.2 — and must
    # not submit to something never scored at all, which is every application
    # until matching lands in Phase 5. No score is not a passing score.
    score = await _match_score(session, application)
    if score is None or score < profile.min_match_score:
        await transition(
            session,
            application,
            ApplicationStatus.NEEDS_REVIEW,
            payload={
                "reason": (
                    "auto-submit is on but this posting has no match score yet"
                    if score is None
                    else "match score is below the profile's threshold"
                ),
                "score": score,
                "min_match_score": profile.min_match_score,
            },
        )
        return

    await _submit(session, application, adapter, page)


async def _submit(
    session: AsyncSession,
    application: Application,
    adapter: Any,
    page: Any,
) -> None:
    """Send the form and record the receipt. The only path that submits."""
    receipt = await adapter.submit(page)
    receipt.screenshot_ref = await _capture(page, application, "submitted.png")
    application.receipt_json = receipt.model_dump()

    await transition(
        session,
        application,
        ApplicationStatus.SUBMITTED,
        payload={"confirmation": receipt.confirmation_text},
    )


async def _fail(
    session: AsyncSession,
    application: Application,
    reason: FailureReason,
    message: str,
) -> None:
    await transition(
        session,
        application,
        ApplicationStatus.FAILED,
        failure_reason=reason,
        payload={"message": message[:500]},
    )


async def park_failed(
    session: AsyncSession,
    application: Application,
    reason: FailureReason,
    message: str,
) -> None:
    """Move an application to failed, if it is still in a state that allows it."""
    status = ApplicationStatus(application.status)
    if status in (ApplicationStatus.SUBMITTED, ApplicationStatus.FAILED):
        return
    if status is not ApplicationStatus.RUNNING:
        await begin_work(session, application)
    await _fail(session, application, reason, message)
