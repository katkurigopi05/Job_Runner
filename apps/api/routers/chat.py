"""The assistant.

A chat surface over the owner's own data, answered by a model running on this
machine. Three things about it are deliberate.

**It is local by default, and remote only when asked, per question.** Chat
context carries application URLs, profile details, and recruiter
correspondence. This route used to refuse remote providers outright, on the
grounds that §2.8 permits exactly one third-party upload — the tailoring call —
and a chat window is not it.

The owner widened that deliberately. `ChatRequest.provider` may name Gemini,
Anthropic or OpenRouter, and naming one sends the context to it. What has *not*
changed is what happens when nobody asks: omitting the field answers locally,
`LLM_PROVIDER` is still ignored here, and no failure path reaches for a cloud
provider on its own. A remote answer requires someone to have chosen it for
that question.

Per request rather than per environment, so the choice is visible where it is
made instead of sitting in `.env` where a later reader would not connect it to
recruiter mail leaving the machine. The reply carries `local` and `model`, so
an answer that cost privacy never looks like one that did not.

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
from packages.core.config import get_settings
from packages.core.enums import ErrorCode
from packages.core.models import Application, InboundMessage, Profile
from packages.core.schemas import ChatReply, ChatRequest
from packages.llm import router as llm_router
from packages.llm.audit import is_local
from packages.llm.prompts import CHAT_SYSTEM
from packages.llm.provider import LLMError

router = APIRouter(prefix="/chat", tags=["chat"])

#: What answers when the request does not say. Still the local model: widening
#: the ceiling did not move the floor, and the common case must not cost
#: privacy by default.
LOCAL_PROVIDER = "ollama"

#: Providers the assistant accepts by name.
#:
#: `stub` is absent on purpose — canned text presented as an answer about the
#: owner's real applications is exactly the failure StubProvider's marker
#: exists to make visible.
ALLOWED_PROVIDERS = ("ollama", "gemini", "anthropic", "openrouter")

#: Defined in packages/llm/prompts.py — see the note there on versioning.
SYSTEM = CHAT_SYSTEM.text


async def _context(
    session: SessionDep, application_id: uuid.UUID | None, *, include_mail: bool
) -> str:
    """Facts the model is allowed to speak from.

    `include_mail` gates the recruiter correspondence — the only thing in here
    sourced from the owner's Gmail, and the most sensitive: it is other
    people's writing about the owner, and they did not choose a provider.
    Always true for the local model, and for a remote one only when the owner
    turned it on for that question.
    """
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

        if include_mail:
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
        else:
            # Said rather than silently omitted. The model is told to answer
            # from what it was handed and to say when it does not know — an
            # absent section would read as "no replies have arrived", which is
            # a different and wrong answer.
            lines.append("  recent replies: withheld — not shared with a remote model")

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

    selected = (body.provider or LOCAL_PROVIDER).strip().lower()
    if selected not in ALLOWED_PROVIDERS:
        raise ApiError(
            ErrorCode.INVALID_REQUEST,
            f"unknown chat provider {selected!r}; expected one of {', '.join(ALLOWED_PROVIDERS)}",
        )

    # Resolved before the context is built, because it decides what goes into
    # it. The local model always sees the mail — it is the owner's own machine
    # and there is nothing to withhold it from. A remote one sees it only if
    # the owner said so for this question, and the default is no: recruiter
    # correspondence is other people's writing, and they did not pick a
    # provider.
    #
    # Keyed on `selected` rather than on the resolved `local` flag on purpose.
    # The `is_local` check below refuses a cloud-served Ollama model outright,
    # so it can never reach the model with mail attached — and deciding this
    # after building the provider would put a network-dependent call between
    # the owner's choice and its effect.
    include_mail = selected == LOCAL_PROVIDER or body.share_mail

    context = await _context(session, body.application_id, include_mail=include_mail)
    prompt = f"CONTEXT:\n{context}\n\nQUESTION:\n{question}"

    try:
        provider = llm_router.build_provider(selected)
        model = getattr(provider, "model", None)
        # Kept even though remote providers are now permitted, because this
        # check was never only about where the answer came from — it is about
        # the label matching. Ollama serves cloud-hosted models over the same
        # localhost API: `kimi-k2.6:cloud` and `qwen3-coder:480b-cloud` are
        # both in this owner's model list, neither runs on this machine, and
        # nothing in the URL says so.
        #
        # Choosing Gemini is an informed decision and is allowed. Asking for
        # the local model and silently getting a third party is not a decision
        # at all, so that one still refuses.
        if selected == LOCAL_PROVIDER and not is_local(selected, model):
            raise ApiError(
                ErrorCode.INVALID_REQUEST,
                f"OLLAMA_MODEL is set to {model!r}, which Ollama serves from its own "
                "servers rather than this machine — so this would not be the local "
                "answer it claims to be. Set OLLAMA_MODEL to a model you have pulled "
                "(llama3.1 is the default), or pick a cloud provider explicitly.",
            )
        # The assistant answers from context it was handed and is told to say
        # when it does not know. Inventing an application status is the exact
        # failure §14 names, so this is the low end deliberately.
        answer = await provider.complete(SYSTEM, prompt, max_tokens=600, temperature=0.2)
    except LLMError as exc:
        # Still no automatic fallback, in either direction. A local model that
        # is down must not silently promote the question to a cloud provider —
        # that would send the context off the machine without anyone choosing
        # it, which is the one thing the per-request switch exists to prevent.
        # A remote provider that fails does not quietly drop to the local one
        # either: the answer would come from a different model than the one
        # asked for, and `LLM_FALLBACK_LOCAL` deliberately does not reach here.
        if selected == LOCAL_PROVIDER:
            raise ApiError(
                ErrorCode.INTERNAL_ERROR,
                f"The local model is not answering ({exc}). Start Ollama with "
                f"`ollama serve`, or pull the configured model with "
                f"`ollama pull {get_settings().ollama_model}`. "
                "Nothing was sent anywhere else — pick a cloud provider explicitly "
                "if you want one.",
            ) from exc
        raise ApiError(
            ErrorCode.INTERNAL_ERROR,
            f"{selected} did not answer ({exc}). Nothing fell back to another model — "
            "ask again, or switch to the local one.",
        ) from exc

    return ChatReply(
        reply=answer,
        provider=selected,
        model=model,
        grounded=True,
        # What actually happened, not what was asked for — the two differ for
        # the local model, which always sees the mail whatever the toggle says.
        shared_mail=include_mail,
        # Computed, never assumed from the provider name: `is_local` is what
        # knows that an Ollama-served `:cloud` model is not local.
        local=is_local(selected, model),
    )
