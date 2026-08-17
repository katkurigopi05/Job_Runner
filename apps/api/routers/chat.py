"""The assistant.

A chat surface over the owner's own data, answered by a model running on this
machine. Three things about it are deliberate.

**It is local-only.** Chat context carries application URLs, profile details,
and recruiter correspondence. §2.8 permits exactly one third-party upload — the
tailoring call — and a chat window is not it. So this route asks for Ollama by
name rather than taking whatever `LLM_PROVIDER` happens to be, and if Ollama is
not running it says so instead of quietly sending the owner's data to a cloud
provider that *is* configured.

**It will not answer for the profile.** §2.2 answers are copied verbatim and
never generated. Asked "what should I put for work authorization", the
assistant refuses and points at the profile — the same boundary the router
enforces for form filling, applied to the conversation.

**It is grounded, not freehand.** The model is given the actual counts and the
actual application, and told to say when it does not know. An assistant that
invents an application status is worse than no assistant.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import func, select

from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from packages.core.enums import ErrorCode
from packages.core.models import Application, InboundMessage, Profile
from packages.core.schemas import ChatReply, ChatRequest
from packages.llm import router as llm_router
from packages.llm.provider import LLMError

router = APIRouter(prefix="/chat", tags=["chat"])

#: The assistant runs on this machine. Not configurable on purpose — see the
#: module docstring.
LOCAL_PROVIDER = "ollama"

SYSTEM = """You are the assistant inside Jobrunner, a local job-application \
agent that belongs to one person. You are talking to that person about their \
own job search.

Ground every answer in the CONTEXT below. If the context does not contain the \
answer, say so plainly — do not guess a status, a company, or a date. Inventing \
one is worse than admitting the gap.

Never draft an answer to a work-authorization, sponsorship, employment-history, \
or salary question. Those are copied word for word from the owner's profile \
because a wrong one has legal consequences. If asked, say that and point them \
at the profile page.

Nothing you say submits anything. Applications are sent only when the owner \
approves them on the review screen.

Be brief. This is a tool, not a chat companion."""


async def _context(session: SessionDep, application_id: uuid.UUID | None) -> str:
    """Facts the model is allowed to speak from."""
    counts = dict(
        (
            await session.execute(
                select(Application.status, func.count()).group_by(Application.status)
            )
        ).all()
    )
    tally = [f"  {status}: {count}" for status, count in sorted(counts.items())]
    lines = ["APPLICATIONS BY STATUS:", *(tally or ["  none yet"])]

    profiles = list((await session.scalars(select(Profile))).all())
    if profiles:
        lines.append("PROFILES:")
        for profile in profiles:
            lines.append(
                f"  {profile.label}: location={profile.location or 'unset'}, "
                f"auto_submit={profile.auto_submit}, min_match_score={profile.min_match_score}"
            )

    if application_id is not None:
        application = await session.get(Application, application_id)
        if application is None:
            raise ApiError(ErrorCode.NOT_FOUND, "application not found")

        lines += [
            "THE APPLICATION BEING ASKED ABOUT:",
            f"  url: {application.url}",
            f"  status: {application.status}",
            f"  ats: {application.ats or 'unknown'}",
            f"  outcome: {application.outcome or 'nothing back yet'}",
        ]

        review = application.review_json or {}
        unanswered = review.get("unanswered") or []
        if unanswered:
            lines.append("  questions it is parked on:")
            lines += [f"    - {q.get('question')}" for q in unanswered]

        messages = list(
            (
                await session.scalars(
                    select(InboundMessage)
                    .where(InboundMessage.application_id == application_id)
                    .order_by(InboundMessage.at.desc())
                    .limit(3)
                )
            ).all()
        )
        if messages:
            lines.append("  recent replies:")
            lines += [f"    - {m.from_addr}: {m.subject or '(no subject)'}" for m in messages]

    return "\n".join(lines)


@router.post("", response_model=ChatReply)
async def chat(body: ChatRequest, session: SessionDep) -> ChatReply:
    """Answer a question about the owner's own job search."""
    question = body.message.strip()
    if not question:
        raise ApiError(ErrorCode.INVALID_REQUEST, "message is empty")

    # §2.2, applied to the conversation rather than to a form field. Refused
    # here rather than left to the system prompt, because a prompt is a request
    # and this is a rule.
    if llm_router.is_protected(question):
        return ChatReply(
            reply=(
                "I do not draft answers for work authorization, sponsorship, employment "
                "history, or salary. Those are copied from your profile word for word, "
                "because a wrong answer on a real application has legal consequences. "
                "Set them on the profile page and the agent will use them exactly."
            ),
            provider="refused",
            grounded=False,
        )

    context = await _context(session, body.application_id)
    prompt = f"CONTEXT:\n{context}\n\nQUESTION:\n{question}"

    try:
        provider = llm_router.build_provider(LOCAL_PROVIDER)
        answer = await provider.complete(SYSTEM, prompt, max_tokens=600)
    except LLMError as exc:
        # Deliberately not falling back to a configured cloud provider: chat
        # context is the owner's own data and §2.8 does not cover it.
        raise ApiError(
            ErrorCode.INTERNAL_ERROR,
            f"The local model is not answering ({exc}). Start Ollama with "
            "`ollama serve`, or pull a model with `ollama pull llama3.1`. "
            "The assistant runs locally on purpose and will not use a cloud provider.",
        ) from exc

    return ChatReply(reply=answer, provider=LOCAL_PROVIDER, grounded=True)
