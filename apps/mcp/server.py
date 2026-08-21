"""MCP server — drive Jobrunner conversationally.

CLAUDE.md §9 puts this phase early on purpose: once these tools exist, every
later phase is testable by conversation instead of by curl.

Two things to know about the tool surface:

- **Nothing here submits an application.** `approve_application` is the human
  approval gate (§2.3); the worker does the submitting, and only when the
  profile has opted in above its match threshold. There is deliberately no
  "submit now" tool.
- **Tool names describe what they actually do.** CLAUDE.md §4 lists a
  `tailor_resume` tool, but tailoring — LLM rewriting behind the fabrication
  guard — is Phase 3 and does not exist yet. What exists is assembly (source
  résumé plus ranked GitHub projects), so the tool is called
  `preview_resume`. Naming it `tailor_resume` would advertise a capability the
  code does not have.
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from apps.mcp.client import ApiCallFailed, ApiUnavailable, JobrunnerClient

INSTRUCTIONS = """
Jobrunner is a local, single-user job-application agent.

Applications never submit without explicit approval. Use `review_queue` to see
what is waiting, and `approve_application` to release one. An application with
unanswered questions carries the employer's exact wording — answer those
verbatim rather than paraphrasing, and never invent an answer to a
work-authorization question.
""".strip()

server = MCPServer(name="jobrunner", instructions=INSTRUCTIONS)

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

    Note the crawler that fills this index is Phase 5; on a current install
    this returns nothing and says so. Apply to a URL directly in the meantime.
    """
    params: dict[str, Any] = {"q": query, "limit": limit}
    if location:
        params["location"] = location
    if ats:
        params["ats"] = ats
    return await _call("GET", "/postings", params=params)


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
    return await _call("GET", f"/applications/{application_id}")


@server.tool()
async def application_history(application_id: str) -> dict[str, Any]:
    """The append-only event log for an application — every state change."""
    events = await _call("GET", f"/applications/{application_id}/events")
    return {"events": events}


@server.tool()
async def list_applications(status: str | None = None, limit: int = 50) -> dict[str, Any]:
    """List applications, optionally filtered by status.

    Statuses: queued, running, needs_review, needs_otp, submitted, failed.
    """
    params: dict[str, Any] = {}
    if status:
        params["status_filter"] = status
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
    result = await _call("GET", "/applications", params={"status_filter": "needs_review"})
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


@server.tool()
async def approve_application(
    application_id: str, answers: dict[str, Any] | None = None, note: str | None = None
) -> dict[str, Any]:
    """Approve a parked application, optionally supplying missing answers.

    `answers` is keyed by the `key` field from `review_queue`. This is the
    human approval gate — only call it when the owner has actually decided.
    Approving resumes the run; it does not itself submit anything.
    """
    return await _call(
        "POST",
        f"/applications/{application_id}/review",
        json={"approve": True, "answers": answers or {}, "note": note},
    )


@server.tool()
async def reject_application(application_id: str, note: str | None = None) -> dict[str, Any]:
    """Reject a parked application. Terminal — it fails as rejected_at_review."""
    return await _call(
        "POST",
        f"/applications/{application_id}/review",
        json={"approve": False, "answers": {}, "note": note},
    )


@server.tool()
async def submit_otp(application_id: str, code: str) -> dict[str, Any]:
    """Supply a verification code to an application parked at needs_otp."""
    return await _call("POST", f"/applications/{application_id}/otp", json={"code": code})


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
    return await _call("GET", f"/resumes/{resume_id}/parsed")


@server.tool()
async def preview_resume(resume_id: str, job_text: str = "", limit: int = 4) -> dict[str, Any]:
    """What an assembled résumé would contain for a given posting.

    Shows which sections survived parsing, which GitHub projects the ranking
    picked, and exactly how each project link will read.

    This is assembly, not tailoring: the source résumé's text is reproduced
    verbatim and only the Projects section is generated. LLM rewriting behind
    the fabrication guard arrives in Phase 3.
    """
    return await _call(
        "POST",
        f"/resumes/{resume_id}/preview",
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
    return await _call("PATCH", f"/projects/{project_id}", json=body)


def main() -> None:
    """Run over stdio, which is how Claude Code connects."""
    server.run()


if __name__ == "__main__":
    main()
