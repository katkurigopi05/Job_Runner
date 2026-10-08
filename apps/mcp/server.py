"""MCP server — drive Jobrunner conversationally.

CLAUDE.md §9 puts this phase early on purpose: once these tools exist, every
later phase is testable by conversation instead of by curl.

Two things to know about the tool surface:

- **Nothing here submits an application.** `approve_application` is the human
  approval gate (§2.3); the worker does the submitting, and only when the
  profile has opted in above its match threshold. There is deliberately no
  "submit now" tool.
- **Tool names describe what they actually do.** CLAUDE.md §4 lists a
  `tailor_resume` tool, and there is none. Tailoring — LLM rewriting behind
  the fabrication guard — is built, but it runs inside the apply pipeline so
  the guard has one caller. What the tools expose is reviewing it:
  `inspect_application_resume`, `compare_tailoring`, `select_tailoring` and
  `edit_application_resume` (guard-checked, because a model is typing).
  `preview_resume` is assembly only — source résumé plus ranked GitHub
  projects — and says so.
- **The owner's two decisions make the client ask.** `approve_application`
  and `submit_otp` are marked so that Claude Code prompts on every call (see
  `ASKS_THE_OWNER`). Their docstrings ask a model to wait for the owner; the
  mark is what makes the client wait.
- **What the owner must supply, the owner types.** The answers an application
  was parked for, and a verification code, are asked for in a form the client
  shows the person (`_owners_answers`, `_owners_code`). Neither is an argument
  of its tool, so a model has nowhere to put one. §2.2 says a model never
  writes a work-authorization answer, and a question the pipeline could not
  map can be exactly that.
- **An id is one part of a path.** Every id a tool is given goes through
  `_id` on its way into a request's path. Written bare, `../resumes/<id>/edit?`
  in place of an application's id sent the guarded résumé edit to the route
  that does not guard.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import Context, Elicit, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field, create_model

from apps.mcp.client import ApiCallFailed, ApiUnavailable, JobrunnerClient

INSTRUCTIONS = """
Jobrunner is a local, single-user job-application agent.

Applications never submit without explicit approval. Use `review_queue` to see
what is waiting, and `approve_application` to release one. An application with
unanswered questions carries the employer's exact wording. You do not answer
them: approving shows the owner a form and they type the answers themselves.
The same goes for a verification code.
""".strip()

server = MCPServer(name="jobrunner", instructions=INSTRUCTIONS)

#: Claude Code shows the permission prompt on every call to a tool whose
#: `tools/list` entry carries this in `_meta`: in every permission mode, past
#: any allow rule, with no "don't ask again". In the one mode that never
#: prompts it refuses the call. The value has to be the JSON boolean.
ASK_FLAG = "anthropic/requiresUserInteraction"

#: The tools that carry it: the two that move a parked application on the
#: owner's say-so (§2.3). Over MCP the caller is a model, and "only call it
#: when the owner has actually decided" is a sentence in a docstring.
#:
#: `.claude/settings.json` holds an ask rule for the same two, for a client
#: build that does not read the flag. `tests/test_mcp.py` keeps this tuple,
#: the decorators below and that file in agreement.
#:
#: `reject_application` is left out. It is terminal, but it sends nothing to
#: an employer, and a prompt on every tool the review gate has would be read
#: past.
#:
#: `start_crawl` is the third, for a different reason. A crawl makes real
#: requests to every employer's board in the registry, and the assistant's
#: "run crawler" is a typed command matched in code, never inferred from a
#: question (§14). Here the one deciding is a model, and an empty feed is
#: exactly what would make one decide to.
ASKS_THE_OWNER: tuple[str, ...] = ("approve_application", "submit_otp", "start_crawl")

_ASK_EVERY_TIME: dict[str, Any] = {ASK_FLAG: True}

#: Replaced in tests to bind the tools to the ASGI app instead of a socket.
_client = JobrunnerClient()


def set_client(client: JobrunnerClient) -> None:
    global _client
    _client = client


def get_client() -> JobrunnerClient:
    return _client


async def _call(method: str, path: str, **kwargs: Any) -> Any:
    """Run a request, turning transport problems into readable tool errors."""
    try:
        return await _client.request(method, path, **kwargs)
    except ApiUnavailable as exc:
        return {"error": str(exc)}
    except ApiCallFailed as exc:
        return {"error": exc.message, "code": exc.code}


#: What an id is made of, as far as a path is concerned. Whether it is
#: anybody's id is the API's to say: every id it issues is a UUID and it
#: answers `invalid_request` to anything else.
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _id(value: str, name: str = "application_id") -> str:
    """An id on its way into a path. Anything that is not one is refused.

    Every path below is written with this. httpx resolves `..` before it
    sends, so `../inbox?` in place of an application's id asked the API for
    `GET /inbox`: the recruiter mail this server has no tool for, on purpose.
    The one calling is a model, and a posting or a reply it has read can tell
    it what to pass. `tests/test_mcp_ids.py` reads this file to keep a tool
    written later from leaving it out.
    """
    if not isinstance(value, str) or not _ID.fullmatch(value):
        # The value is not repeated back: it may be the instruction that put it there.
        raise ToolError(
            f"`{name}` is not an id, so nothing was asked of Jobrunner. "
            "Pass one a listing tool returned."
        )
    return value


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@server.tool()
async def detect_ats(url: str) -> dict[str, Any]:
    """Identify which applicant tracking system is behind a posting URL.

    Pure URL-pattern matching, no network call. Returns the ATS name and
    whether Jobrunner has an adapter for it.
    """
    return await _call("POST", "/detect", json={"url": url})


@server.tool()
async def supported_ats() -> dict[str, Any]:
    """List the applicant tracking systems Jobrunner can drive."""
    result = await _call("GET", "/ats")
    return {"supported": result}


@server.tool()
async def search_postings(
    query: str = "", location: str | None = None, ats: str | None = None, limit: int = 20
) -> dict[str, Any]:
    """Search postings Jobrunner has indexed.

    The index is filled by the crawler polling the company registry. An
    empty result means nothing matched, or no crawl has run yet — `make crawl`
    runs one, and the setup page reports when the last one finished. A URL
    can always be applied to directly with `apply_to_url`.
    """
    params: dict[str, Any] = {"q": query, "limit": limit}
    if location:
        params["location"] = location
    if ats:
        params["ats"] = ats
    return await _call("GET", "/postings", params=params)


#: What a match is cut down to. The feed's own row also carries the rubric, the
#: legitimacy findings and the parsed requirements, several thousand characters
#: a posting, and a model listing ten matches would be handed a page for each.
_MATCH_FIELDS = (
    "id",
    "posting_id",
    "title",
    "location",
    "url",
    "score",
    "personalized_score",
    "decision",
    "closed",
    "first_seen_at",
    "matched_terms",
    "missing_terms",
    "excluded_by",
    "eligibility",
    "compensation",
)


@server.tool()
async def my_matches(
    profile_id: str | None = None,
    role: str | None = None,
    keywords: str = "",
    locations: str = "",
    remote: bool | None = None,
    posted_within_days: int | None = None,
    min_salary: float | None = None,
    wanted_skills: str = "",
    sponsorship: str | None = None,
    include_unknown_sponsorship: bool = False,
    exclude_citizenship_restricted: bool = False,
    include_applied: bool | None = None,
    undecided_only: bool = False,
    rank: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """The owner's scored feed: postings matched to their profile, best first.

    Not `search_postings`, which searches everything crawled. This is what the
    dashboard's matches page shows, with the same filters. A filter narrows
    what is listed and is never typed onto an application.

    `keywords`, `locations` and `wanted_skills` are comma-separated. `role` is
    one of the feed's role names and a misspelt one is refused. `sponsorship`
    takes `available`, which keeps only postings that state it; add
    `include_unknown_sponsorship` to drop only the ones that refuse it.
    `rank` is `base` or `personalized`. Leave a filter out to keep the owner's
    standing preference for it, such as their search area.

    `score` is a similarity and is low by construction: a strong match can be
    under 0.1. Say where a posting ranks, not a percentage. `eligibility` is
    what the posting itself states about sponsorship and citizenship;
    "unstated" means it said nothing, not that it is available.
    """
    asked: dict[str, Any] = {"limit": limit}
    # Only what was given. Several of the feed's defaults are the owner's
    # standing preferences, and sending this tool's own default for each would
    # overrule them without anyone having asked.
    when_set = {
        "profile_id": profile_id,
        "role": role,
        "keywords": keywords,
        "locations": locations,
        "posted_within_days": posted_within_days,
        "min_salary": min_salary,
        "wanted_skills": wanted_skills,
        "sponsorship": sponsorship,
        "rank": rank,
        # These two can be given as false, which is an answer.
        "remote": remote,
        "include_applied": include_applied,
    }
    asked.update({name: value for name, value in when_set.items() if value not in (None, "")})
    switches = {
        "include_unknown_sponsorship": include_unknown_sponsorship,
        "exclude_citizenship_restricted": exclude_citizenship_restricted,
        "undecided_only": undecided_only,
    }
    asked.update({name: True for name, on in switches.items() if on})

    rows = await _call("GET", "/matches", params=asked)
    if not isinstance(rows, list):
        return rows

    listed: dict[str, Any] = {
        "matches": [{field: row.get(field) for field in _MATCH_FIELDS} for row in rows],
        "count": len(rows),
        "asked": asked,
    }
    if not rows:
        listed["note"] = (
            "Nothing in the feed fits. Either no posting passes these filters, or nothing "
            "has been scored yet: matches are written when a crawl finishes. A filter on "
            "something a posting does not state hides that posting."
        )
    return listed


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------


@server.tool()
async def apply_to_url(candidate_id: str, profile_id: str, url: str) -> dict[str, Any]:
    """Queue an application for a posting URL.

    This only enqueues work. The worker fills the form and stops at a review
    screen; nothing reaches the employer without approval.

    Fails with `invalid_request` if the profile is missing anything an ATS form
    requires (phone, location, work authorization, résumé) — those are knowable
    now, so an application that cannot be completed is never created.
    """
    return await _call(
        "POST",
        "/applications",
        json={"candidate_id": candidate_id, "profile_id": profile_id, "url": url},
    )


@server.tool()
async def application_status(application_id: str) -> dict[str, Any]:
    """Current status of one application, with its review record."""
    return await _call("GET", f"/applications/{_id(application_id)}")


@server.tool()
async def application_history(application_id: str) -> dict[str, Any]:
    """The append-only event log for an application — every state change."""
    events = await _call("GET", f"/applications/{_id(application_id)}/events")
    return {"events": events}


@server.tool()
async def list_applications(status: str | None = None, limit: int = 50) -> dict[str, Any]:
    """List applications, optionally filtered by status.

    Statuses: queued, running, needs_review, needs_otp, submitted, failed.
    """
    params: dict[str, Any] = {}
    if status:
        params["status"] = status
    result = await _call("GET", "/applications", params=params or None)
    if isinstance(result, list):
        return {"applications": result[:limit], "count": len(result)}
    return result


# --------------------------------------------------------------------------
# The approval gate
# --------------------------------------------------------------------------


@server.tool()
async def review_queue() -> dict[str, Any]:
    """Applications parked and waiting for a decision.

    Each carries the employer's questions in their original wording under
    `review.unanswered`. Answer those exactly — a paraphrase is not something
    the owner can safely stand behind.
    """
    result = await _call("GET", "/applications", params={"status": "needs_review"})
    if not isinstance(result, list):
        return result

    queue = []
    for application in result:
        review = application.get("review") or {}
        queue.append(
            {
                "application_id": application["id"],
                "url": application["url"],
                "ats": application["ats"],
                "fill_rate": review.get("fill_rate"),
                "unanswered": [
                    {
                        "key": q["key"],
                        "question": q["question"],
                        "kind": q["kind"],
                        "options": [o["label"] for o in q.get("options", [])],
                    }
                    for q in review.get("unanswered", [])
                ],
                "screenshot_ref": review.get("screenshot_ref"),
            }
        )
    return {"waiting": queue, "count": len(queue)}


#: The dashboard to send the owner to when a form cannot reach them.
_REVIEW_PAGE = "http://127.0.0.1:3001/review"


class _NothingOpen(BaseModel):
    """What the answers resolver hands on when no question is open. No form is shown."""


class _Code(BaseModel):
    code: str = Field(title="Verification code", description="The code the site sent you.")


def _must_reach_the_owner(ctx: Context, what: str) -> None:
    """Refuse, with somewhere to go, when this client cannot show a form.

    The library would refuse too, with a message about capabilities. This one
    says what to do. The check is the library's own: a bare `elicitation` is
    form support, and one that offers only links is not.
    """
    capabilities = ctx.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    if elicitation is not None and (elicitation.form is not None or elicitation.url is None):
        return
    raise ToolError(
        f"This client cannot show the owner a form, and {what} must be typed by them. "
        f"Nothing was approved or sent. The owner can do this at {_REVIEW_PAGE}."
    )


def _form_for(questions: list[dict[str, Any]]) -> type[BaseModel]:
    """One field to a question, titled with the employer's exact wording.

    A question with choices offers those choices and no others: §8's rule that
    a dropdown answer must match an option the employer gave, applied before
    the answer exists. A multi-select stays free text, as it is on `/review`,
    because a form field here holds one value.

    Each field writes itself out under the employer's key, so what the owner
    typed is posted as the review endpoint expects it without a second lookup.
    """
    fields: dict[str, Any] = {}
    for number, question in enumerate(questions, start=1):
        labels = [option["label"] for option in question.get("options") or []]
        one_of_them = bool(labels) and question.get("kind") != "multi_select"
        fields[f"q{number}"] = (
            Literal[tuple(labels)] if one_of_them else str,  # type: ignore[valid-type]
            Field(
                title=question["question"],
                description=None if one_of_them or not labels else "Choices: " + "; ".join(labels),
                serialization_alias=question["key"],
            ),
        )
    return create_model("Answers", **fields)


async def _owners_answers(application_id: str, ctx: Context) -> Elicit[BaseModel] | _NothingOpen:
    """Ask the owner the questions this application was parked for.

    Runs before the tool, in place of an argument a model could fill. With
    nothing open there is nothing to ask and no form.
    """
    application = await _call("GET", f"/applications/{_id(application_id)}")
    if not isinstance(application, dict) or "error" in application:
        raise ToolError(
            str(application.get("error") if isinstance(application, dict) else application)
        )
    questions = (application.get("review") or {}).get("unanswered") or []
    if not questions:
        return _NothingOpen()
    _must_reach_the_owner(ctx, "the answers to an employer's questions")
    return Elicit(
        f"This application was left with {len(questions)} question(s) for you. "
        "What you type goes on the employer's form as you typed it.",
        _form_for(questions),
    )


async def _owners_code(application_id: str, ctx: Context) -> Elicit[_Code]:
    # Before the form: an id that will be refused is not worth a code typed for it.
    _id(application_id)
    _must_reach_the_owner(ctx, "a verification code")
    return Elicit("The site sent you a verification code. Type it here.", _Code)


@server.tool(meta=_ASK_EVERY_TIME)
async def approve_application(
    application_id: str,
    answers: Annotated[ElicitationResult[BaseModel], Resolve(_owners_answers)],
    note: str | None = None,
) -> dict[str, Any]:
    """Approve a parked application. The owner supplies any missing answers.

    If the application was left with unanswered questions, the owner is shown
    a form carrying the employer's wording and types the answers. You do not
    pass them, and there is no argument to pass them in. This is the human
    approval gate — only call it when the owner has actually decided.
    Approving resumes the run; it does not itself submit anything.
    """
    if not isinstance(answers, AcceptedElicitation):
        return {
            "approved": False,
            "error": (
                "Not approved: the owner closed the form without answering. Nothing was "
                f"guessed in its place. They can answer at {_REVIEW_PAGE}."
            ),
        }
    typed = answers.data.model_dump(by_alias=True)
    return await _call(
        "POST",
        f"/applications/{_id(application_id)}/review",
        json={
            "approve": True,
            # A field left empty is still an open question, not an answer.
            "answers": {key: value for key, value in typed.items() if str(value).strip()},
            "note": note,
        },
    )


@server.tool()
async def reject_application(application_id: str, note: str | None = None) -> dict[str, Any]:
    """Reject a parked application. Terminal — it fails as rejected_at_review."""
    return await _call(
        "POST",
        f"/applications/{_id(application_id)}/review",
        json={"approve": False, "answers": {}, "note": note},
    )


@server.tool(meta=_ASK_EVERY_TIME)
async def submit_otp(
    application_id: str,
    code: Annotated[ElicitationResult[_Code], Resolve(_owners_code)],
) -> dict[str, Any]:
    """Resume an application parked at needs_otp. The owner types the code.

    The code is asked for in a form; you do not pass it.
    """
    if not isinstance(code, AcceptedElicitation):
        return {
            "submitted": False,
            "error": "No code was sent: the owner closed the form without typing one.",
        }
    return await _call(
        "POST", f"/applications/{_id(application_id)}/otp", json={"code": code.data.code}
    )


@server.tool()
async def compare_tailoring(application_id: str, cloud: str | None = None) -> dict[str, Any]:
    """Tailor this posting with the local model and the cloud one, for a choice.

    Answers "would the other model have written a better résumé for this job",
    which is otherwise only answerable by editing `.env` and re-running.

    Costs a real upload. Each remote side sends the owner's résumé to a third
    party, so call it when the owner has asked to compare — not routinely, and
    not to explore. Asking twice for the same posting sends nothing: the
    tailoring cache is keyed per provider.

    Both sides are checked by the fabrication guard before either is returned,
    and `rejected` counts what it refused. A side that could not run — no key,
    spent allowance, Ollama not started — comes back with `error` set rather
    than missing, because a comparison silently down to one column reads as a
    verdict on the column that is there.

    `cloud` names the remote half — "gemini", "anthropic", "openrouter" — for
    this comparison only. Leave it out for whatever real tailoring would use,
    which answers the usual question. Naming one moves no setting: the next
    application tailors exactly as it did before. It exists because OpenRouter
    is deliberately not in the automatic order, so comparing against it
    otherwise meant adopting it for everything first.

    Requires the application to be parked at needs_review. Nothing here changes
    which résumé is sent; `select_tailoring` does that.
    """
    result = await _call(
        "POST",
        f"/applications/{_id(application_id)}/tailoring/compare",
        json={"cloud": cloud},
    )
    if not isinstance(result, dict):
        return result

    review = result.get("review") or {}
    sides = review.get("tailoring_comparison") or []
    return {
        "application_id": result.get("id"),
        "currently_attached": result.get("tailored_resume_id"),
        "candidates": [
            {
                "requested": side.get("requested"),
                # What answered, which differs from what was asked when the
                # remote allowance ran out and the local model took over.
                "answered_by": side.get("answered_by"),
                "resume_id": side.get("resume_id"),
                "changed": side.get("changed"),
                "unchanged": side.get("unchanged"),
                "rejected_by_guard": side.get("rejected"),
                "reused": side.get("reused"),
                "error": side.get("error"),
                "changes": side.get("changes"),
            }
            for side in sides
        ],
    }


@server.tool()
async def select_tailoring(application_id: str, resume_id: str) -> dict[str, Any]:
    """Choose which compared résumé this application will upload.

    `resume_id` comes from `compare_tailoring`. The API refuses anything that
    was not one of the compared versions — this decides the file an employer
    receives, and the screen only ever offers two.

    Selecting is not approving. The application stays parked until
    `approve_application` is called.
    """
    return await _call(
        "POST",
        f"/applications/{_id(application_id)}/tailoring/select",
        json={"resume_id": resume_id},
    )


@server.tool()
async def inspect_application_resume(application_id: str) -> dict[str, Any]:
    """The résumé this application will actually upload, line by line.

    The tailored one when tailoring has run, otherwise the profile's base — the
    same rule the uploader applies, answered by the API rather than worked out
    here, so a tool and the worker cannot disagree about which document is
    being sent.

    Call this before `edit_application_resume`: sections are replaced whole, so
    an edit has to be built from the current set rather than guessed.
    """
    return await _call("GET", f"/applications/{_id(application_id)}/resume")


@server.tool()
async def edit_application_resume(
    application_id: str,
    sections: dict[str, list[str]],
    contact_name: str | None = None,
    contact_email: str | None = None,
    contact_phone: str | None = None,
    contact_links: list[str] | None = None,
    adopt: bool = False,
) -> dict[str, Any]:
    """Edit the résumé an application is about to send. Guarded — see below.

    **Relay the owner's words; do not compose résumé content.** This writes a
    document that goes to a real employer under the owner's name. Use it when
    they have told you what to change — a line to drop, a phrasing they dictated,
    a typo to fix — not to improve their résumé on their behalf.

    The fabrication guard runs on every edit that arrives through this tool, and
    it is not optional here. §2.1 permits rephrasing, reordering and
    re-emphasis; it forbids adding a skill, employer, date, credential or metric
    the résumé does not already support. A refused edit comes back naming the
    lines and the unsupported claims.

    That refusal is not always a verdict on the fact. If the owner says it is
    true and the guard still refuses, the answer is for them to type it on the
    `/review` screen — an edit made there is theirs, and is deliberately not
    guarded. Do not try to reword it past the check.

    `sections` replaces the whole set, so start from `inspect_application_resume`
    and send it back modified. The `contact_*` fields do the same — one left as
    None is *cleared*, not kept, so pass back every one the résumé should keep.

    Only applications parked at needs_review may be edited, and the edit is
    attached to this application alone — `adopt=True` also makes it the
    profile's base, which is only right for a fix that is true for every future
    application.

    Editing does not approve. The application stays parked, and the edit
    survives approval rather than being re-tailored over.
    """
    result = await _call(
        "POST",
        f"/applications/{_id(application_id)}/resume/edit",
        json={
            "contact": {
                "name": contact_name,
                "email": contact_email,
                "phone": contact_phone,
                "links": contact_links or [],
            },
            "sections": sections,
            "adopt": adopt,
            # Never settable from here. The author on this path is a model, and
            # the whole reason this flag exists is that the API cannot tell.
            "guard": True,
        },
    )
    if not isinstance(result, dict) or "error" in result:
        return result

    review = result.get("review") or {}
    pinned = review.get("resume_pinned") or {}
    return {
        "application_id": result.get("id"),
        "resume_id": result.get("tailored_resume_id"),
        "from_version": pinned.get("from_version"),
        "version": pinned.get("version"),
        "adopted_as_base": adopt,
        "status": result.get("status"),
        "note": (
            "Attached to this application only. It stays parked — call "
            "approve_application when the owner has decided."
        ),
    }


# --------------------------------------------------------------------------
# The tracker
# --------------------------------------------------------------------------
#
# Read only. Adding a task, ticking one off and linking a contact stay on the
# dashboard: the record of who the owner is in touch with is theirs to keep.

#: What a task is cut down to in a list. Its notes and its meeting link come
#: with `application_tracking`, which is one application's and not everyone's.
_TASK_FIELDS = (
    "id",
    "application_id",
    "application_url",
    "kind",
    "title",
    "due_at",
    "overdue",
    "source",
)

#: A contact is somebody else. Over MCP the reader is a model that is not on
#: this machine, and they never chose that; §14 says the same of their mail.
#: Who they are is sent. How to reach them, and what the owner wrote about
#: them, is not.
_CONTACT_FIELDS = ("name", "company", "role")

_SILENT_FIELDS = ("application_id", "url", "days_since", "has_follow_up_task")


def _listed_task(task: dict[str, Any]) -> dict[str, Any]:
    left = [item["text"] for item in task.get("checklist") or [] if not item.get("done")]
    return {**{field: task.get(field) for field in _TASK_FIELDS}, "checklist_left": left}


@server.tool()
async def follow_ups(within_days: int = 7, silent_after_days: int | None = None) -> dict[str, Any]:
    """What is waiting on the owner in the tracker: tasks, and unanswered applications.

    Open tasks come in groups: `overdue`, `due` in the next `within_days`,
    `undated`, and a count of the ones due `later`. An undated task is not a
    low priority. An interview or assessment task made from a recruiter's
    reply has no date until the owner reads it out of the message and sets it.

    `silent` is the submitted applications no employer has answered, longest
    wait first. `has_follow_up_task` means the owner already has it in hand.
    `stale` counts the ones too old for a follow-up to be worth sending.
    `silent_after_days` moves how long an application waits before it is
    listed; leave it out for the report's own. `suggested_silent_after_days`
    is that wait worked out from the owner's own history, and is null until
    enough employers have answered.

    Reports only. Jobrunner sends nothing to an employer: a follow-up is the
    owner's to write and to send.
    """
    # The window is the route's to draw, so this has no clock of its own: what
    # the windowed list holds is due or overdue, and the rest is told apart by
    # whether it has a date at all.
    windowed = await _call("GET", "/tasks", params={"due_within_days": within_days})
    if not isinstance(windowed, list):
        return windowed
    every_open = await _call("GET", "/tasks")
    if not isinstance(every_open, list):
        return every_open
    # Only when given. The report's default is its own to hold.
    asked = {} if silent_after_days is None else {"silent_after_days": silent_after_days}
    report = await _call("GET", "/analytics/cadence", params=asked or None)
    if "silent" not in report:
        return report

    in_window = {task["id"] for task in windowed}
    beyond = [task for task in every_open if task["id"] not in in_window]
    waiting: dict[str, Any] = {
        "overdue": [_listed_task(task) for task in windowed if task.get("overdue")],
        "due": [_listed_task(task) for task in windowed if not task.get("overdue")],
        "undated": [_listed_task(task) for task in beyond if task.get("due_at") is None],
        "later": sum(1 for task in beyond if task.get("due_at") is not None),
        "silent": [
            {field: item.get(field) for field in _SILENT_FIELDS}
            for item in report["silent"]
            if not item.get("stale")
        ],
        "stale": report.get("stale", 0),
        "suggested_silent_after_days": (report.get("latency") or {}).get(
            "suggested_silent_after_days"
        ),
        "within_days": within_days,
    }
    if not any(waiting[group] for group in ("overdue", "due", "undated", "silent")):
        waiting["note"] = (
            "Nothing is waiting. A task exists once the owner adds one or a recruiter's "
            "reply is routed to its application, and an application is listed as unanswered "
            "only once it has been submitted and has waited the full time."
        )
    return waiting


@server.tool()
async def application_tracking(application_id: str) -> dict[str, Any]:
    """One application's tasks, and the people the owner is in touch with about it.

    A task comes whole: its checklist, its notes, where the interview is. A
    contact is cut to name, relationship, company and role. Their email,
    phone and profile link, and the owner's notes on them, are on the
    dashboard and are not sent here.
    """
    tracking = await _call("GET", f"/applications/{_id(application_id)}/tracking")
    if "contacts" not in tracking:
        return tracking
    return {
        "contacts": [
            {
                **{field: linked["contact"].get(field) for field in _CONTACT_FIELDS},
                "relationship": linked.get("relationship"),
            }
            for linked in tracking["contacts"]
        ],
        "tasks": tracking["tasks"],
    }


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------
#
# Four that read, and one that starts a crawl. The setup page's registry
# repair is not here: it rewrites company rows, and the owner previews it
# first on that page.

#: What the audit summary is cut down to. The trail keeps digests so that the
#: owner, holding the original, can prove what was sent; a digest is no use to
#: an assistant, and the listing that carries them has no tool.
_AUDIT_FIELDS = (
    "total_calls",
    "uploads",
    "uploaded_chars",
    "by_provider",
    "by_task",
    "first_at",
    "last_at",
)

#: A setup item without its `facts` and its page `actions`.
_SETUP_FIELDS = ("key", "title", "group", "state", "detail", "steps")


@server.tool()
async def audit_trail(days: int = 7) -> dict[str, Any]:
    """What left this machine in the last `days`: how many calls, to whom, for what.

    Every call to a model is recorded, local ones too. `uploads` are the ones
    whose text went to a third party, `uploaded_chars` is how much, and
    `by_provider` and `by_task` count those uploads. Tailoring a résumé is the
    upload the owner agreed to; one under another task is worth pointing out.

    Sizes and counts only. The trail holds no prompt text, so none can be
    shown, and this cannot say what a call contained.
    """
    summary = await _call("GET", "/audit/summary", params={"days": days})
    if "uploads" not in summary:
        return summary
    left: dict[str, Any] = {field: summary.get(field) for field in _AUDIT_FIELDS}
    left["days"] = days
    if not summary["uploads"]:
        left["note"] = (
            f"Nothing left this machine in the last {days} days. "
            f"{summary.get('total_calls', 0)} call(s) were answered by a model on it."
        )
    return left


@server.tool()
async def setup_health() -> dict[str, Any]:
    """Whether this installation is working, and what to do about what is not.

    `overall` is the worst state of anything checked. `needs_attention` is
    each thing that is not fine, with why and the steps to try in order; the
    steps are for the owner to run. `ok` names what is fine. No check repeats
    a password or a key.
    """
    status = await _call("GET", "/setup/status")
    if "items" not in status:
        return status
    return {
        "overall": status.get("overall"),
        "needs_attention": [
            {field: item.get(field) for field in _SETUP_FIELDS}
            for item in status["items"]
            if item.get("state") != "ok"
        ],
        "ok": [item.get("title") for item in status["items"] if item.get("state") == "ok"],
    }


@server.tool()
async def weekly_digest(
    window_days: int | None = None, profile_id: str | None = None
) -> dict[str, Any]:
    """The last week in numbers: postings seen, applications made, replies, what is waiting.

    `window_days` changes the week to another length. `quiet_week` is true
    when nothing happened at all, which more often means the crawler stopped
    than that the market did: check `crawl_status`. A rate under `funnel` is
    null until enough applications stand behind it.
    """
    # Only what was given. The report's week is its own to define.
    asked = {
        name: value
        for name, value in (("window_days", window_days), ("profile_id", profile_id))
        if value is not None
    }
    return await _call("GET", "/analytics/digest", params=asked or None)


@server.tool()
async def crawl_status() -> dict[str, Any]:
    """Whether the crawler is working, and how fresh the postings are.

    `stalled` means a crawl is waiting and no worker is running to do it: the
    owner starts one with `make worker`. `newest_posting_at` is what answers
    "are my postings current"; a crawl that ran and found nothing leaves it
    unchanged.
    """
    return await _call("GET", "/crawl/status")


@server.tool(meta=_ASK_EVERY_TIME)
async def start_crawl() -> dict[str, Any]:
    """Start a crawl of the company registry, so new postings reach the feed.

    Only when the owner asks for one. Do not start one because a search or the
    feed came back empty: say so and let them decide. A crawl makes real
    requests to employers' sites, inside the rate limits, and takes a while.

    One crawl runs at a time. If one is already waiting or running, nothing is
    queued and `queued` is false.
    """
    started = await _call("POST", "/crawl")
    if "queued" not in started:
        return started
    if not started["queued"]:
        note = "A crawl is already queued or running, so another was not started."
    elif started.get("worker_alive"):
        note = (
            "Crawl started. New postings reach the feed over the next few minutes, and "
            "matching runs when the sweep finishes."
        )
    else:
        note = "Crawl queued."
    if not started.get("worker_alive"):
        note += " No worker is running to do it: the owner starts one with `make worker`."
    return {**started, "note": note}


# --------------------------------------------------------------------------
# Profile, résumé, projects
# --------------------------------------------------------------------------


@server.tool()
async def list_candidates() -> dict[str, Any]:
    """List candidates. Most operations need a candidate_id from here."""
    result = await _call("GET", "/candidates")
    return {"candidates": result}


@server.tool()
async def list_profiles() -> dict[str, Any]:
    """List profiles — the reusable answer sets applications are made from."""
    result = await _call("GET", "/profiles")
    return {"profiles": result}


@server.tool()
async def list_resumes(candidate_id: str) -> dict[str, Any]:
    """List uploaded résumés for a candidate, newest version first."""
    result = await _call("GET", "/resumes", params={"candidate_id": candidate_id})
    return {"resumes": result}


@server.tool()
async def inspect_resume(resume_id: str) -> dict[str, Any]:
    """What the parser extracted from a résumé, section by section.

    Worth checking before applying: a section missing here is one an ATS
    reading the same file may also miss.
    """
    return await _call("GET", f"/resumes/{_id(resume_id, 'resume_id')}/parsed")


@server.tool()
async def preview_resume(resume_id: str, job_text: str = "", limit: int = 4) -> dict[str, Any]:
    """What an assembled résumé would contain for a given posting.

    Shows which sections survived parsing, which GitHub projects the ranking
    picked, and exactly how each project link will read.

    This is assembly, not tailoring: the source résumé's text is reproduced
    verbatim and only the Projects section is generated. Tailoring itself runs
    in the apply pipeline behind the fabrication guard; review its output with
    `inspect_application_resume` or `compare_tailoring`.
    """
    return await _call(
        "POST",
        f"/resumes/{_id(resume_id, 'resume_id')}/preview",
        params={"job_text": job_text, "limit": limit},
    )


@server.tool()
async def sync_github_projects(
    candidate_id: str, username: str, include_private: bool = False
) -> dict[str, Any]:
    """Import the owner's GitHub repositories as résumé project material.

    Re-running updates in place. Your `pinned` and `include` choices are never
    overwritten by a sync.
    """
    return await _call(
        "POST",
        "/projects/sync/github",
        json={
            "candidate_id": candidate_id,
            "username": username,
            "include_private": include_private,
        },
    )


@server.tool()
async def list_projects(candidate_id: str) -> dict[str, Any]:
    """List imported projects for a candidate."""
    result = await _call("GET", "/projects", params={"candidate_id": candidate_id})
    return {"projects": result}


@server.tool()
async def preview_projects(candidate_id: str, job_text: str = "", limit: int = 4) -> dict[str, Any]:
    """Which projects would go on a résumé for this posting, and why.

    Returns each project's score so the ranking is inspectable rather than
    something you have to trust.
    """
    result = await _call(
        "POST",
        "/projects/preview",
        params={"candidate_id": candidate_id, "job_text": job_text, "limit": limit},
    )
    return {"selected": result}


@server.tool()
async def curate_project(
    project_id: str, pinned: bool | None = None, include: bool | None = None
) -> dict[str, Any]:
    """Pin a project onto every résumé, or exclude it from all of them.

    `pinned=True` guarantees it appears; `include=False` removes it from
    consideration regardless of ranking.
    """
    body: dict[str, Any] = {}
    if pinned is not None:
        body["pinned"] = pinned
    if include is not None:
        body["include"] = include
    return await _call("PATCH", f"/projects/{_id(project_id, 'project_id')}", json=body)


def main() -> None:
    """Run over stdio, which is how Claude Code connects."""
    server.run()


if __name__ == "__main__":
    main()
