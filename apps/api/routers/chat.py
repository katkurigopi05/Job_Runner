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

**Questions about postings are answered from retrieved postings.** A question
that asks about jobs or names a company is searched against the open postings
on this machine (`packages/matching/retrieve.py`), and the matches go into the
context with labels the model cites. The reply reports which were cited, read
out of the text rather than taken on the model's word, and how much of the
corpus the search covered — "no Kafka roles" over a sixth of the postings is
not the same answer as over all of them.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, replace
from typing import Any

from fastapi import APIRouter
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import chat_history
from apps.api.chat_budget import BudgetReport, PromptBudget, pack
from apps.api.chat_facts import (
    ApplicationFact,
    CompanyApplications,
    OwnerFacts,
    asks_for_application_snapshot,
    asks_for_counts,
    count_facts,
    render_postings,
)
from apps.api.chat_figures import (
    asks_for_application_rates,
    asks_for_chart,
    chart_reply,
    rates_section,
)
from apps.api.chat_selection import (
    EvidenceItem,
    SelectionResult,
    parse_selection,
    render_selection,
)
from apps.api.deps import SessionDep
from apps.api.errors import ApiError
from apps.api.protected_questions import (  # noqa: F401
    asks_for_a_protected_answer,
    is_refused,
    names_a_protected_field,
)
from apps.api.routers.chat_evidence import skill_lines, skills_asked_about, unsupported_citations
from apps.worker.crawl_job import request_crawl
from packages.ats.registry import detect_ats
from packages.core import heartbeat
from packages.core.config import Settings, get_settings
from packages.core.enums import ErrorCode
from packages.core.heartbeat import WorkerState
from packages.core.models import Application, Company, InboundMessage, Posting, Profile
from packages.core.notify import ParkReason, asks, needs_owner
from packages.core.schemas import (
    ChatKind,
    ChatPostingShare,
    ChatRelatedRole,
    ChatReply,
    ChatRequest,
    ChatSource,
)
from packages.crawler.discover import slug_from_ats_url
from packages.llm import router as llm_router
from packages.llm.audit import is_local
from packages.llm.prompts import CHAT_SYSTEM
from packages.llm.provider import (
    LLMError,
    ModelMissing,
    ModelTimedOut,
    ModelUnreachable,
    PromptTooLong,
)
from packages.matching.gaps import GapReport, asked_and_unmet, skill_label, target_gaps
from packages.matching.level import LevelCount
from packages.matching.retrieve import Passage, Retrieval, asks_about_own_applications, retrieve
from packages.matching.roles import display_name
from packages.matching.sponsored import SponsorshipCount
from packages.matching.tiers import RELATED_PREFIX

router = APIRouter(prefix="/chat", tags=["chat"])

#: How many kinds of title the model is told about by name. A word in 222
#: titles has dozens of kinds; the largest few say what the list is made of.
_KINDS_IN_CONTEXT = 8

#: What answers when the request does not say. Still the local model: widening
#: the ceiling did not move the floor, and the common case must not cost
#: privacy by default.
LOCAL_PROVIDER = "ollama"

#: Providers the assistant accepts by name.
#:
#: `stub` is absent on purpose — canned text presented as an answer about the
#: owner's real applications is exactly the failure StubProvider's marker
#: exists to make visible.
#: `ollama_cloud` is here as a *separate name* from `ollama`, which is the
#: whole mechanism. The refusal below still fires when someone sets
#: `OLLAMA_MODEL` to a `:cloud` tag and asks for the local model, because that
#: is a remote answer wearing a local label. Choosing `ollama_cloud` in the
#: picker is the informed decision §14 asks for, and the reply's `local: false`
#: says so to the UI.
ALLOWED_PROVIDERS = (
    "ollama",
    "gemini",
    "anthropic",
    "openrouter",
    "tokenrouter",
    "ollama_cloud",
    "kaggle",
)

#: Defined in packages/llm/prompts.py — see the note there on versioning.
SYSTEM = CHAT_SYSTEM.text


def _one_line(text: object) -> str:
    """Somebody else's text, as one line of the context and never more.

    The context is lines, and some are the app's own: `APPLICATIONS BY
    STATUS:`, `WAITING ON YOU:`, `[P1] …`. A posting's title, a form's
    question and a reply's subject are written by an employer or a sender, and
    one that brings a line break with it writes lines of its own under those,
    in the same voice. The prompt tells the model the context is material and
    not instruction, which is advice. This is the part that is code: whatever
    such text says, it says inside the one line it was given.

    `str.split` with no argument splits on every whitespace character Unicode
    has, the line and paragraph separators among them.
    """
    return " ".join(str(text or "").split())


def _application_fact(application: Application, title: str | None = None) -> ApplicationFact:
    return ApplicationFact(
        title=_one_line(title or application.url),
        url=_one_line(application.url),
        status=application.status,
        failure_reason=application.failure_reason,
        created=application.created_at.isoformat(),
        updated=application.updated_at.isoformat(),
    )


async def _read_context(
    session: SessionDep, application_id: uuid.UUID | None, *, include_mail: bool
) -> OwnerFacts:
    """Facts the model is allowed to speak from.

    `include_mail` gates the recruiter correspondence — the only thing in here
    sourced from the owner's Gmail, and the most sensitive: it is other
    people's writing about the owner, and they did not choose a provider.
    Always true for the local model, and for a remote one only when the owner
    turned it on for that question.
    """
    # By reason as well as status: `failed: 4` cannot say that three of them
    # are blocked forms waiting to be finished by hand, and "what needs me?"
    # answered "1 needs review" on the owner's database while they were.
    rows = (
        await session.execute(
            select(Application.status, Application.failure_reason, func.count()).group_by(
                Application.status, Application.failure_reason
            )
        )
    ).all()
    counts: dict[str, int] = {}
    parked: dict[ParkReason, int] = {}
    for status, failure_reason, count in rows:
        counts[status] = counts.get(status, 0) + count
        if (reason := needs_owner(status, failure_reason)) is not None:
            parked[reason] = parked.get(reason, 0) + count
    tally = [f"  {status}: {count}" for status, count in sorted(counts.items())]
    waiting = [f"  {r.value}: {parked[r]} ({asks(r)})" for r in ParkReason if r in parked]
    lines = [
        "APPLICATIONS BY STATUS:",
        *(tally or ["  none yet"]),
        "WAITING ON YOU:",
        # The same applications, listed again by what they need. Handed both
        # lists bare, the local model answered "one needs your review.
        # Additionally, there is one in the review category": one application.
        *(
            ["  (already counted above: these say which of them need you, not more applications)"]
            if waiting
            else []
        ),
        *(waiting or ["  nothing"]),
    ]

    profiles = list((await session.scalars(select(Profile))).all())
    if profiles:
        lines.append("PROFILES:")
        for profile in profiles:
            lines.append(
                f"  {profile.label}: location={profile.location or 'unset'}, "
                f"auto_submit={profile.auto_submit}, min_match_score={profile.min_match_score}"
            )

    selected_application = None
    if application_id is not None:
        application = await session.get(Application, application_id)
        if application is None:
            raise ApiError(ErrorCode.NOT_FOUND, "application not found")
        selected_application = _application_fact(application)

        lines += [
            "THE APPLICATION BEING ASKED ABOUT:",
            f"  url: {_one_line(application.url)}",
            f"  status: {application.status}",
            f"  ats: {application.ats or 'unknown'}",
            f"  outcome: {application.outcome or 'nothing back yet'}",
        ]

        review = application.review_json or {}
        unanswered = review.get("unanswered") or []
        if unanswered:
            lines.append("  questions it is parked on:")
            lines += [f"    - {_one_line(q.get('question'))}" for q in unanswered]

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
                lines += [
                    f"    - {_one_line(m.from_addr)}: {_one_line(m.subject) or '(no subject)'}"
                    for m in messages
                ]
        else:
            # Said rather than silently omitted. The model is told to answer
            # from what it was handed and to say when it does not know — an
            # absent section would read as "no replies have arrived", which is
            # a different and wrong answer.
            lines.append("  recent replies: withheld — not shared with a remote model")

    return OwnerFacts(
        context="\n".join(lines),
        statuses=tuple(sorted(counts.items())),
        waiting=tuple((reason, parked[reason]) for reason in ParkReason if reason in parked),
        application=selected_application,
    )


async def _context(
    session: SessionDep, application_id: uuid.UUID | None, *, include_mail: bool
) -> str:
    """Text projection retained for context inspection and callers."""
    return (await _read_context(session, application_id, include_mail=include_mail)).context


async def _company_application_facts(
    session: AsyncSession, companies: tuple[str, ...]
) -> tuple[CompanyApplications, ...]:
    """Application facts for named employers, including unlinked board URLs.

    A linked posting is authoritative. Only an application with no posting
    may fall back to its URL's ATS and exact registry board slug.
    """
    employers = list(
        (await session.scalars(select(Company).where(Company.name.in_(companies)))).all()
    )
    rows = (
        await session.execute(
            select(Application, Posting.title, Posting.company_id)
            .outerjoin(Posting, Application.posting_id == Posting.id)
            .where(
                or_(
                    Posting.company_id.in_([c.id for c in employers]),
                    Application.posting_id.is_(None),
                )
            )
            .order_by(Application.created_at, Application.id)
        )
    ).all()
    facts = []
    for company in employers:
        slug = company.slug or slug_from_ats_url((company.careers_url or "").rstrip("/") + "/")
        matched = [
            (application, title)
            for application, title, company_id in rows
            if company_id == company.id
            or (
                application.posting_id is None
                and slug is not None
                and detect_ats(application.url) == company.ats_type
                and (slug_from_ats_url(application.url) or "").casefold() == slug.casefold()
            )
        ]
        facts.append(
            CompanyApplications(
                name=_one_line(company.name),
                applications=tuple(
                    _application_fact(application, title) for application, title in matched
                ),
            )
        )
    return tuple(facts)


async def _company_applications(session: AsyncSession, companies: tuple[str, ...]) -> str:
    """Company-specific context from the same typed records as fixed answers."""
    return "\n".join(
        fact.context() for fact in await _company_application_facts(session, companies)
    )


#: Questions about what the owner is missing. Only these get the résumé-derived
#: section: elsewhere it is noise, and for a remote provider it is the owner's
#: skill list sent for no reason.
_ASKS_ABOUT_GAPS = re.compile(
    r"\b(?:lag(?:ging)?|lack(?:s|ing)?|missing|gaps?|weak(?:ness(?:es)?)?|improve|upskill"
    r"|learn|résumés?|resumes?|cv|skills?|strengths?|qualif\w*)\b",
    re.IGNORECASE,
)

#: What makes one of those words a question about the owner: the first
#: person, or their résumé. "me" is left out, as it is for protected answers
#: below, and for the same sentence: "show me".
_ABOUT_THE_OWNER = re.compile(
    r"\b(?:i|i'm|im|i've|ive|my|mine|myself|we|our|résumés?|resumes?|cv)\b", re.IGNORECASE
)

#: And what makes it one about postings instead.
_ABOUT_POSTINGS = re.compile(
    r"\b(?:jobs?|roles?|postings?|positions?|openings?|vacanc(?:y|ies)|listings?"
    r"|employers?|compan(?:y|ies))\b",
    re.IGNORECASE,
)

#: Enough of each list to answer from, not a dump of the vocabulary.
_GAPS_SHOWN = 12
_COVERED_SHOWN = 8


#: Politeness and filler a command may open with: "can you run the crawler?".
_POLITE = r"(?:(?:please|pls|hey|ok|okay|go|now|can you|could you|would you|will you)\s+)*"

#: An instruction to look for new postings, as the owner phrases it. Anchored to
#: the start and to an imperative verb, so a question *about* jobs ("Which new
#: jobs mention Kafka?") or about the crawler ("Did the crawler run?") is never
#: read as one — those are searched or answered, not acted on.
_CRAWL_COMMAND = re.compile(
    rf"^\s*{_POLITE}(?:"
    r"(?:run|start|kick\s*off|trigger|launch|begin)\s+(?:the\s+|a\s+|another\s+|new\s+)?"
    r"(?:job\s+)?crawl(?:er|ing)?\b"
    r"|crawl(?:er)?(?:\s+(?:now|again|jobs|postings|the\s+registry))*\s*[.!?]*\s*$"
    r"|(?:find|fetch|get|pull|grab|look\s+for|check\s+for|search\s+for|scan\s+for)\s+"
    r"(?:some\s+|any\s+|me\s+|the\s+)?(?:new|fresh|latest)\s+"
    r"(?:jobs?|postings?|roles?|openings?|listings?)\b"
    r"|(?:refresh|update|reload|sync)\s+(?:the\s+|my\s+)?"
    r"(?:jobs?|postings?|feed|listings?|job\s+board)\b"
    r")",
    re.IGNORECASE,
)


def asks_to_crawl(message: str) -> bool:
    return bool(_CRAWL_COMMAND.search(message))


async def _start_crawl(session: SessionDep) -> ChatReply:
    """Queue a crawl through `request_crawl` and say plainly what happens next.

    No model: a command is not a question, and nothing about it needs leaving
    the machine. Whether a worker is alive is in the reply, because a crawl
    queued with nobody to run it looks exactly like a registry with nothing new.
    """
    requested = await request_crawl(session, trigger="manual")
    await session.commit()
    alive = any(w.state is WorkerState.ALIVE for w in await heartbeat.workers(session))
    if requested.queued is None:
        reply = "A crawl is already queued or running, so I did not start another." + (
            " New postings reach your feed as the worker gets through the registry."
            if alive
            else " No worker is running to do it, though: start one with `make worker`."
        )
    elif alive:
        reply = (
            "Crawl started. The worker is running and polls each company's board within "
            "its rate limits, so new postings reach your feed over the next few minutes; "
            "matching runs when the sweep finishes."
        )
    else:
        reply = (
            "Crawl queued, but no worker is running, so nothing happens until you start "
            "one with `make worker`."
        )
    return ChatReply(reply=reply, provider="crawler", grounded=True, local=True, shared_mail=False)


def asks_about_gaps(question: str) -> bool:
    """Whether the owner is asking what they themselves are missing.

    One of the gap words, and the question is about the owner or about no
    posting at all. The words alone were the whole test, and "skills",
    "learn", "improve" and "missing" are in questions about postings too:
    "Which jobs require Python skills?" put the owner's résumé skills in the
    context, and sent them to a remote provider when one was picked, for a
    question that was not about the owner.
    """
    if not _ASKS_ABOUT_GAPS.search(question):
        return False
    return bool(_ABOUT_THE_OWNER.search(question)) or not _ABOUT_POSTINGS.search(question)


def _gaps_section(report: GapReport) -> str:
    """The owner's skills against what their top matches ask for.

    Skill names and counts only. The résumé text stays on this machine: this
    context can go to a remote provider, and §2.8 permits one résumé upload —
    tailoring — which a chat question is not.
    """
    if not report.resumes:
        return "SKILL GAPS: no résumé uploaded, so there is nothing to compare against"
    lines = ["YOUR RÉSUMÉS — the skills read from each (their text is not included):"]
    for resume in report.resumes:
        labels = sorted((skill_label(k) for k in resume.skills), key=str.lower)
        lines.append(f"  {resume.label}: {', '.join(labels) or 'no skills recognised'}")
    total = report.read + report.unread
    if not total:
        lines.append("SKILL GAPS: no open matches yet to compare against")
        return "\n".join(lines)
    # What it was counted over, in the heading, with the two larger numbers
    # it is not a count of. Handed "required by 12" under "searched 11,147
    # postings", a model said twelve of eleven thousand.
    lines.append(
        f"SKILL GAPS — what your top {total} open match{'' if total == 1 else 'es'} "
        f"ask{'s' if total == 1 else ''} for, read from each posting's requirements. "
        f"Counted over those {total} only: the best-scoring of the {report.matched} open "
        f"postings matched to you, not all {report.open_postings} open postings:"
    )
    if report.unread:
        lines.append(f"  {report.unread} have no requirements read yet and are not counted")
    lines.append("  not on any résumé:")
    lines += [
        f'    {d.label}: required by {d.required}, preferred by {d.preferred} — e.g. "{d.example}"'
        for d in report.missing[:_GAPS_SHOWN]
    ] or ["    nothing — every skill they ask for is on a résumé"]
    lines.append("  already on a résumé:")
    lines += [
        f"    {d.label}: required by {d.required}, preferred by {d.preferred}"
        f" — on {', '.join(d.on_resumes)}"
        for d in report.covered[:_COVERED_SHOWN]
    ] or ["    none of them"]
    return "\n".join(lines)


@dataclass(frozen=True)
class _PostingGaps:
    #: Per retrieved posting: what it asks for, and what no résumé lists.
    lines: dict[uuid.UUID, str]
    #: The same, counted across all of them, for the top of the section.
    across: str | None


async def _requirements_of(
    session: AsyncSession, found: Retrieval
) -> dict[uuid.UUID, dict[str, Any] | None]:
    """Current requirements for supplied postings, refreshed in memory when stale.

    Only stale or missing selected readings need their raw descriptions. The
    stored extraction stays unchanged; missing source text remains unknown.
    """
    if not found.passages:
        return {}

    from packages.matching.requirements import EXTRACTOR_VERSION, extract

    rows = await session.execute(
        select(Posting.id, Posting.requirements_json, Posting.requirements_version).where(
            Posting.id.in_([passage.posting_id for passage in found.passages])
        )
    )
    readings: dict[uuid.UUID, dict[str, Any] | None] = {}
    stale: list[uuid.UUID] = []
    for posting_id, requirements, version in rows.all():
        if requirements is not None and version == EXTRACTOR_VERSION:
            readings[posting_id] = requirements
        else:
            readings[posting_id] = None
            stale.append(posting_id)
    if stale:
        descriptions = await session.execute(
            select(Posting.id, Posting.description_raw).where(Posting.id.in_(stale))
        )
        for posting_id, description in descriptions.all():
            if description and description.strip():
                readings[posting_id] = extract(description).as_json()
    return readings


def _unmet_by_posting(
    found: Retrieval, report: GapReport, requirements_by_id: dict[uuid.UUID, dict[str, Any] | None]
) -> _PostingGaps:
    """What the retrieved postings ask for that no résumé lists, each and together.

    The together half exists because of a measured loss. Asked "What am I
    missing for Kafka jobs?" on 2026-10-04, a LangGraph agent searched and then
    built "Terraform: missing in the Faire, New Relic and Anthropic postings"
    itself, in three model calls; this route had every per-posting line in its
    context and answered from the feed-wide table. The summary is a count, so
    it is computed here and put first.
    """
    if not found.passages or not report.resumes:
        return _PostingGaps({}, None)
    have = frozenset().union(*(resume.skills for resume in report.resumes))
    lines: dict[uuid.UUID, str] = {}
    where: dict[str, list[str]] = {}
    for passage in found.passages:
        requirements = requirements_by_id.get(passage.posting_id)
        if requirements is None:
            lines[passage.posting_id] = "requirements: not read yet"
            continue
        asked, unmet = asked_and_unmet(requirements, have)
        lines[passage.posting_id] = (
            f"asks for: {', '.join(asked) or 'no skills it names as required or preferred'}\n"
            f"       not on your résumés: {', '.join(unmet) or 'nothing it asks for'}"
        )
        for label in unmet:
            where.setdefault(label, []).append(passage.label)
    ranked = sorted(where, key=lambda label: (-len(where[label]), label.lower()))
    across = "  across these postings, asked for and on none of your résumés: " + (
        ", ".join(f"{label} ({', '.join(where[label])})" for label in ranked)
        or "nothing — every skill they ask for is on a résumé"
    )
    return _PostingGaps(lines, across)


def _beyond_the_role_line(found: Retrieval, count: int) -> str:
    """How many of a role's postings say what else the question asked.

    They are listed first and the rest of the role after them (`tiers.py`),
    so the count is what keeps "614 software engineer postings" from being
    read as 614 in AI.
    """
    words = " ".join(f'"{word}"' for word in found.beyond_role)
    said = found.beyond_in_title + (found.beyond_mentioned or 0)
    where = (
        "in the title or the description"
        if found.beyond_mentioned is not None
        else ("in the title")
    )
    if not said:
        return (
            f"  asked for beyond the role: {words}. None of the {count} says it {where}, so "
            "below are the closest of the role. Say that none says it."
        )
    parts = [f"{found.beyond_in_title} have {words} in the title"]
    if found.beyond_mentioned:
        parts.append(f"{found.beyond_mentioned} more mention it in the description")
    return (
        f"  asked for beyond the role: {words}. Of the {count}, {' and '.join(parts)}; the "
        f"postings below are from these. The other {count - said} do not say it {where}, and "
        "the owner sees them listed after, closest first. Give both numbers, and do not count "
        f"the other {count - said} as having {words}."
    )


def _level_lines(count: LevelCount) -> list[str]:
    """How the postings found stand against the level asked, as the model is told it.

    The count of what was left out is the point. Half the titles in the
    owner's area name no level, so "23 senior data engineer postings" is true
    only beside "17 more name no level", and which of the two the owner sees
    listed is their setting (`CHAT_ALLOW_UNKNOWN_SENIORITY`). Either way the
    model is told that a title which does not say is not known to be that
    level: handed a list after a question about senior jobs, it will otherwise
    call them all senior.
    """
    total = count.at_level + count.unknown + count.other
    if not (count.unknown or count.other):
        return [f"  level asked for: {count.said}. All {total} found are at that level by title."]
    lines = [
        f"  level asked for: {count.said}. Of the {total} found, {count.at_level} at that "
        f"level by title, {count.unknown} naming no level, {count.other} naming another level.",
        "  A title that names another level is never listed.",
    ]
    if not count.unknown:
        return lines
    if count.unknown_listed:
        lines.append(
            "  The ones naming no level are listed after the ones at that level: "
            f"do not call one {count.said}, say its title names no level."
        )
    else:
        lines.append(
            "  The ones naming no level are not listed. Say how many there are: "
            f"a title that does not say is not known to be {count.said}."
        )
    return lines


def _sponsorship_lines(count: SponsorshipCount) -> list[str]:
    """What the postings asked about state on sponsorship, as the model is told it.

    All three numbers, because the true answer is often the short one: "one
    of four states that it sponsors, and one states that it does not". And the
    instruction that goes with them: a posting that does not say has not said
    yes (§16, §18), and a model handed a list of jobs after a question about
    sponsorship will otherwise call them all sponsoring.
    """

    def said(number: int, one: str, many: str) -> str:
        return f"{number} {one if number == 1 else many}"

    total = count.offered + count.refused + count.unstated
    quiet = (
        said(count.unstated, "mentions it without saying", "mention it without saying")
        if count.mentions_only
        else said(count.unstated, "does not say", "do not say")
    )
    lines = [
        f"  sponsorship: asked for. Of the {total} {count.over}, "
        + said(count.offered, "states that it sponsors", "state that they sponsor")
        + ", "
        + said(count.refused, "states that it does not", "state that they do not")
        + f", {quiet}.",
        "  Only the ones that state they sponsor are listed below. A posting that does not "
        "say may or may not sponsor: never present one as sponsoring.",
    ]
    if count.mentions_only:
        lines.append("  Every other open posting says nothing about sponsorship.")
    if not count.offered:
        lines.append("  None states that it sponsors: say so, and give the numbers above.")
    return lines


def _postings_section(
    found: Retrieval,
    gaps: _PostingGaps | None = None,
    evidence: dict[uuid.UUID, list[str]] | None = None,
    *,
    excerpt_labels: frozenset[str] | None = None,
) -> str:
    """The retrieved postings, labelled for citation, with the search's reach.

    Coverage comes first and is stated even when nothing was found, because
    the model is told to say how much was searched — it cannot say a number it
    was never given.

    A question that is not about postings says so rather than omitting the
    section, for the reason the withheld mail does: an absent section reads as
    "no postings matched", which is a different answer.
    """
    if not found.attempted:
        if found.unnamed:
            # Nothing was looked for, which is not the same as nothing being
            # there. The owner can be told what to add.
            return (
                "POSTINGS: not searched — the question names no role, skill, company or "
                "place to look for. Say so if asked which jobs, and ask what to look for."
            )
        return "POSTINGS: not searched — the question does not ask about jobs or a company"
    total = found.searched + found.unsearchable
    lines = [f"POSTINGS: searched {found.searched} of {total} open postings"]
    if found.unsearchable:
        lines[0] += f" ({found.unsearchable} are not searchable yet)"
    if found.share is not None:
        share = found.share
        percent = (
            f"{share.percentage:.2f}%"
            if share.percentage is not None
            else "percentage undefined because there are no open postings"
        )
        lines.append(
            f"  computed posting share: {share.matching} of {share.open_total} open postings "
            f"in {share.scope} ({percent}). Repeat this computed share and its denominator; "
            "it is not a résumé match score."
        )
    elif found.share_requested:
        lines.append(
            "  percentage unavailable: this compound search has no complete matching total. "
            "Do not derive a percentage from the ranked sample or the unfiltered role count."
        )
    # Stated whenever it applies, and what it left out with it: "nothing at
    # Faculty" and "nothing at Faculty where you search" are different answers.
    if found.area:
        lines.append(
            f"  search area: {found.area}"
            + (
                f"; {found.outside_area} matching posting"
                f"{'' if found.outside_area == 1 else 's'} " + "outside it left out"
                if found.outside_area
                else ""
            )
        )
    elif found.area_waived:
        lines.append("  search area: not applied, the question names a place outside it")
    if found.place_total is not None:
        reach = "" if found.place_counts_complete else " among retrieved candidates"
        lines.append(
            f"  place filter applied — in the Bay Area: {found.place_total}{reach}; "
            f"{found.search_area_total} matching postings in your search area{reach} "
            "before this filter."
        )
        lines.append(
            f"  {found.remote_or_unplaced_not_listed} remote or unplaced roles "
            "in your search area are not listed "
            f"because they do not establish a Bay Area office{reach}."
        )
    if found.work_mode:
        lines.append(
            "Work mode filter: remote-work evidence is required; "
            "eligibility conditions still apply."
        )
    if found.required_skill is not None:
        lines.append(found.required_skill.in_words())
    # With a level asked, the totals below count the postings listed for it,
    # and the level line after them says how many were not.
    listed_at_level = (
        ""
        if found.level is None
        else (
            " and is listed for the level asked"
            if (found.role_total or found.title_total or 0) == 1
            else " and are listed for the level asked"
        )
    )
    if found.roles:
        # The model sees the closest few. Without the count it cannot say
        # there are more, and three shown out of sixty-one read as "three".
        count = found.role_total or 0
        breakdown = ", ".join(f"{number} {_one_line(kind)}" for kind, number in found.kinds)
        lines.append(
            f"  role asked for: {' or '.join(found.roles)}. {count} open posting"
            f"{'' if count == 1 else 's'} in the search area "
            f"{'has' if count == 1 else 'have'} such a title{listed_at_level}"
            + (f" ({breakdown})" if len(found.kinds) > 1 else "")
            + f"; the closest {len(found.passages)} are below and the owner sees the rest "
            "listed by kind under your answer. Say how many there are of each kind."
        )
        if found.beyond_role:
            lines.append(_beyond_the_role_line(found, count))
    if found.title_words:
        # Searched by title because every word asked for is in most
        # descriptions. The model sees five; the count is what stops "five"
        # being read as "all", and the kinds are what the list is sorted by.
        count = found.title_total or 0
        words = " and ".join(f'"{word}"' for word in found.title_words)
        shown = found.kinds[:_KINDS_IN_CONTEXT]
        breakdown = ", ".join(f"{number} {_one_line(kind)}" for kind, number in shown)
        if len(found.kinds) > len(shown):
            breakdown += f", and {len(found.kinds) - len(shown)} more kinds"
        lines.append(
            f"  titles searched: {count} open posting{'' if count == 1 else 's'} in the search "
            f"area {'has' if count == 1 else 'have'} {words} in the title{listed_at_level}"
            + (f" ({breakdown})" if len(found.kinds) > 1 else "")
            + f"; the closest {len(found.passages)} are below and the owner sees the rest "
            "listed by kind under your answer. Say how many there are and name the main kinds."
        )
        if found.related_total:
            # Listed after the titles with the word, and never handed over:
            # they do not say it, so nothing below may be cited for it.
            related = [
                f"{number} {_one_line(kind.removeprefix(RELATED_PREFIX))}"
                for kind, number in found.kinds
                if kind.startswith(RELATED_PREFIX)
            ]
            lines.append(
                f"  related: {found.related_total} more open posting"
                f"{'' if found.related_total == 1 else 's'} of a role named with {words} "
                f"({', '.join(related)}) do not have it in the title. The owner sees them "
                "listed after the others; none is below. Say they are listed, and do not count "
                f"them among the postings with {words} in the title."
            )
    if found.not_hiring:
        # The question says a word that is also a company's name, and that
        # company has nothing open. The word was searched for, in case a tool
        # was meant; the owner may have meant the employer, so this says so.
        one = len(found.not_hiring) == 1
        lines.append(
            f"  {', '.join(map(_one_line, found.not_hiring))} {'is' if one else 'are'} also "
            f"{'a company' if one else 'companies'} on the owner's list, with no open postings"
            f"{'' if one else ' each'}. What was searched for is the word, in every "
            "employer's postings."
        )
    if not found.passages:
        within = " in the search area" if found.outside_area else ""
        lines.append(
            "  none in the Bay Area"
            if found.place_total is not None
            else (
                f"  no open postings at {', '.join(map(_one_line, found.companies))}{within}"
                if found.companies
                else (
                    "  none in the search area"
                    if found.outside_area
                    else "  none shared anything with this question"
                )
            )
        )
    if found.level is not None:
        lines += _level_lines(found.level)
    if found.sponsorship is not None:
        lines += _sponsorship_lines(found.sponsorship)
    if not (found.roles or found.title_words or found.sponsorship):
        # Totals use the same area rule as ranking. The displayed list stays
        # separate: experimental semantic candidates need not say the term.
        if found.match_total is not None:
            scope = (
                "in the Bay Area"
                if found.place_total is not None
                else "in the search area"
                if found.area
                else "with search-area filtering not applied"
            )
            if found.matched_term is not None:
                lines.append(
                    f"  counted matches: {found.match_total} postings "
                    f"at {found.company_total} companies that say {_one_line(found.matched_term)} "
                    f"{scope}. These count mentions, not requirements."
                )
            else:
                employers = ", ".join(map(_one_line, found.companies))
                lines.append(
                    f"  counted matches: {employers}'s {found.match_total} open postings {scope}."
                )
            lines.append(
                f"  below: the closest {len(found.passages)} retrieved postings. "
                "Required/preferred evidence covers only these supplied postings; "
                "do not describe the whole count as requiring a skill."
            )
        elif found.passages:
            # Other searches still have a capped pool, never a known total.
            lines.append(
                f"  below: the closest {len(found.passages)} of those found. More may match, "
                "and how many do is not known: give no total."
            )
    if gaps is not None and gaps.across:
        lines.append(gaps.across)
    for passage in found.passages:
        # An employer's words, each kept to the line it is given (`_one_line`).
        where = " — ".join(_one_line(part) for part in (passage.company, passage.location) if part)
        lines.append(
            f"  [{passage.label}] {_one_line(passage.title)}" + (f" — {where}" if where else "")
        )
        if passage.application_status:
            lines.append(f"       you applied: {passage.application_status}")
        lines.append(
            f"       excerpt: {passage.excerpt}"
            if excerpt_labels is None or passage.label in excerpt_labels
            else "       excerpt: withheld to fit the context budget"
        )
        # Under the excerpt, which shows the word and not how it is asked for.
        lines += [
            f"       {_one_line(line)}" for line in (evidence or {}).get(passage.posting_id, [])
        ]
        if gaps is not None and passage.posting_id in gaps.lines:
            lines.append(f"       {gaps.lines[passage.posting_id]}")
    return "\n".join(lines)


#: Square brackets, and the lenticular 【】 some models cite with — Nemotron on
#: OpenRouter wrote 【P1】, and a citation the parser cannot see is shown as
#: one the answer did not use.
_BRACKETED = re.compile(r"\[([^\]]*)\]|【([^】]*)】")
#: A label opening a line, optionally as a list item or in bold, then a colon
#: or a dash: "- P1: Waymo …". Nemotron listed every RAG posting this way, with
#: a quote from each, and none of them counted.
_LIST_LABEL = re.compile(r"^[ \t]*(?:[-*•]|\d+[.)])?[ \t]*\**P(\d+)\**[ \t]*[:–—-]", re.MULTILINE)
#: Round brackets holding labels and nothing else: "(P1)", "(P2, P4)". Qwen3
#: 8B on this machine cites this way, and an answer resting on four postings
#: had all five filed under "not cited". Not "(the P2 band)": that is prose.
_ROUND = re.compile(r"\(\s*(P\d+(?:\s*,\s*P\d+)*)\s*\)")
_LABEL = re.compile(r"\bP(\d+)\b")


def cited_labels(reply: str) -> set[str]:
    """Labels the reply cites, in any of the shapes a model writes them.

    "[P1]", "[P1][P3]" and "[P1, P3]" all occur, "(P1)" from the local model,
    and "- P1: …" opening a list line. Otherwise a bare "P1" does not count:
    it can be a salary band or a priority, and reporting it as a citation
    would mark a source as evidence the answer never used.
    """
    bracketed = {
        f"P{number}"
        for groups in _BRACKETED.findall(reply)
        for group in groups
        for number in _LABEL.findall(group)
    }
    rounded = {f"P{number}" for group in _ROUND.findall(reply) for number in _LABEL.findall(group)}
    return bracketed | rounded | {f"P{number}" for number in _LIST_LABEL.findall(reply)}


# The assistant answers from context it was handed and is told to say
# when it does not know. Inventing an application status is the exact
# failure §14 names, so this is the low end deliberately.
ANSWER_TOKENS = 600
ANSWER_TEMPERATURE = 0.2


@dataclass(frozen=True)
class Ready:
    """A question with its context read. All that is left is to ask a model."""

    selected: str
    #: What actually went into the context, which is not always what was asked.
    include_mail: bool
    prompt: str
    found: Retrieval
    #: How many postings the skill-gap table was counted over, when there was one.
    gap_counted: int | None = None
    #: What of the conversation went with the question, and the postings its
    #: earlier answers cited, read again.
    remembered: chat_history.Remembered = chat_history.Remembered()
    earlier: tuple[Passage, ...] = ()
    #: Mail is only read for an application.
    mail_in_context: bool = False
    #: Only explicit evaluation callers opt into packing; live routes leave it off.
    budget: BudgetReport | None = None
    #: Rendered by code from scoped facts; no provider is needed for this answer.
    factual_answer: str | None = None
    #: A posting answer accepts only allowed source IDs, never model prose.
    selection_evidence: tuple[EvidenceItem, ...] | None = None
    selection_summary: str = ""


async def prepare(
    body: ChatRequest, session: AsyncSession, *, context_budget: PromptBudget | None = None
) -> ChatReply | Ready:
    """Everything that happens before a model is asked, for both routes.

    The refusals and the crawl command answer here, with no model. Anything
    else comes back with its context built and nothing left to read from the
    database.
    """
    question = body.message.strip()
    if not question:
        raise ApiError(ErrorCode.INVALID_REQUEST, "message is empty")

    # §2.2, applied to the conversation rather than to a form field. Refused
    # here rather than left to the system prompt, because a prompt is a request
    # and this is a rule.
    if is_refused(question):
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

    # "run crawler", "find new jobs": a command, acted on in code before any
    # provider is chosen, so it reaches no model whichever one is selected.
    if asks_to_crawl(question):
        return await _start_crawl(session)

    # "chart my applications by status": drawn from the database in code. A
    # model may repeat numbers and may derive none, so it is not asked to
    # draw any (`apps/api/chat_figures.py`).
    if asks_for_chart(question):
        return await chart_reply(session, question)

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

    owner_facts = await _read_context(session, body.application_id, include_mail=include_mail)
    context = owner_facts.context
    found = await retrieve(session, question)
    # A rate of the owner's applications is computed here and handed over. If
    # retrieval also took "what percentage" as a posting share it could not
    # count, its "percentage unavailable" line would contradict the rates.
    wants_rates = asks_for_application_rates(question)
    if wants_rates and found.share is None and found.share_requested:
        found = replace(found, share_requested=False)
    gaps = await target_gaps(session) if asks_about_gaps(question) else None
    named_skills = skills_asked_about(question)
    requirements = await _requirements_of(session, found) if (gaps or named_skills) else {}
    posting_gaps = _unmet_by_posting(found, gaps, requirements) if gaps else None
    company_facts = (
        await _company_application_facts(session, found.companies)
        if found.companies and asks_about_own_applications(question)
        else ()
    )
    sections = [
        context,
        *(["\n".join(fact.context() for fact in company_facts)] if company_facts else []),
        _postings_section(found, posting_gaps, skill_lines(found, named_skills, requirements)),
    ]
    postings_index = len(sections) - 1
    if gaps is not None:
        sections.append(_gaps_section(gaps))
    if wants_rates:
        sections.append(await rates_section(session))

    # The conversation so far. `include_mail` already says whether mail may go
    # to this model, and an earlier answer written with mail in hand is mail.
    remembered = chat_history.remember(body.history, question=question, mail_may_go=include_mail)
    earlier = await chat_history.earlier_postings(
        session, remembered.posting_ids, found_now=found.passages
    )
    if earlier:
        sections.append(chat_history.postings_section(earlier))
    said = chat_history.section(remembered)

    def render(excerpt_labels: frozenset[str] | None = None) -> str:
        rendered = list(sections)
        if excerpt_labels is not None:
            rendered[postings_index] = _postings_section(
                found,
                posting_gaps,
                skill_lines(found, named_skills, requirements),
                excerpt_labels=excerpt_labels,
            )
        return (
            "CONTEXT:\n"
            + "\n".join(rendered)
            + (f"\n\n{said}" if said else "")
            + f"\n\nQUESTION:\n{question}"
        )

    factual_answer = None
    if gaps is None and not wants_rates and not remembered.turns and not earlier:
        if company_facts and asks_for_application_snapshot(question):
            factual_answer = "\n".join(fact.answer() for fact in company_facts)
        elif (
            not found.attempted
            and body.application_id is None
            and asks_for_application_snapshot(question)
        ):
            factual_answer = owner_facts.answer()
        elif found.attempted and (asks_for_counts(question) or found.required_skill is not None):
            factual_answer = render_postings(found, skill_lines(found, named_skills, requirements))
    selection_evidence = None
    selection_summary = ""
    if (
        factual_answer is None
        and found.attempted
        and gaps is None
        and not wants_rates
        and not asks_about_own_applications(question)
        and body.application_id is None
    ):
        selection_evidence = tuple(
            EvidenceItem(id=p.label, text=p.excerpt or p.title, source_label=p.label)
            for p in (*found.passages, *earlier)
        )
        selection_summary = render_postings(found, include_sources=False)
        if not selection_evidence:
            factual_answer = selection_summary

        def render_selection_prompt(excerpt_labels: frozenset[str] | None = None) -> str:
            allowed = [
                item
                for item in selection_evidence or ()
                if excerpt_labels is None or item.id in excerpt_labels
            ]
            context = _postings_section(
                found,
                posting_gaps,
                skill_lines(found, named_skills, requirements),
                excerpt_labels=excerpt_labels,
            )
            if earlier:
                context += "\n" + chat_history.postings_section(
                    tuple(p for p in earlier if excerpt_labels is None or p.label in excerpt_labels)
                )
            return (
                "EVIDENCE_SELECTION\nCONTEXT:\n"
                + context
                + "\n"
                + json.dumps(
                    {
                        "question": question,
                        "evidence": [
                            {"id": item.id, "source_label": item.source_label} for item in allowed
                        ],
                    },
                    ensure_ascii=False,
                )
            )

        render = render_selection_prompt
        # The selection path supplies postings only, never mailbox,
        # application, resume or previous model-written context.
        include_mail = False
    budget_report = None
    if context_budget is None:
        prompt = render()
    else:
        prompt, budget_report = pack(
            system=SYSTEM,
            render=render,
            labels=[p.label for p in (*found.passages, *earlier)],
            budget=context_budget,
            answer_tokens=ANSWER_TOKENS,
        )
        if selection_evidence is not None:
            removed = set(budget_report.removed_excerpts)
            selection_evidence = tuple(
                item for item in selection_evidence if item.id not in removed
            )
            if not selection_evidence:
                factual_answer = selection_summary
    return Ready(
        selected=selected,
        include_mail=include_mail,
        prompt=prompt,
        found=found,
        gap_counted=(gaps.read + gaps.unread) if gaps is not None and gaps.resumes else None,
        remembered=remembered,
        earlier=earlier,
        mail_in_context=include_mail and body.application_id is not None,
        budget=budget_report,
        factual_answer=factual_answer,
        selection_evidence=selection_evidence,
        selection_summary=selection_summary,
    )


def provider_for(selected: str) -> tuple[Any, str | None]:
    """The provider that was asked for, refused if it is not what its name says."""
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
            f"({Settings.model_fields['ollama_model'].default} is the default), "
            "or pick a cloud provider explicitly.",
        )
    return provider, model


def _local_remedy(exc: LLMError) -> str:
    """What to do about the local model, for the failure it actually was.

    One sentence used to carry both remedies whatever had happened. A model
    that was not pulled told the owner to start a daemon that had just
    answered them, and a daemon that was not running told them to pull a
    model they had.
    """
    settings = get_settings()
    if isinstance(exc, ModelUnreachable):
        return (
            f"Nothing is listening at {exc.base_url or settings.ollama_base_url}. "
            "Start Ollama with `ollama serve`."
        )
    if isinstance(exc, ModelMissing):
        model = exc.model or settings.ollama_model
        return (
            f"Ollama is running and does not have that model. Get it with "
            f"`ollama pull {model}`, or set OLLAMA_MODEL to one it has."
        )
    if isinstance(exc, ModelTimedOut):
        return (
            "Ollama was reached and did not reply in time: it may still be loading "
            "the model, or this machine is short of memory. Ask again in a moment."
        )
    return (
        "Start Ollama with `ollama serve`, or pull the configured model with "
        f"`ollama pull {settings.ollama_model}`."
    )


def did_not_answer(selected: str, exc: LLMError) -> ApiError:
    """What the owner is told when the model they asked for fails."""
    # Ollama is running and refused: the advice for a model that is down is wrong here.
    if isinstance(exc, PromptTooLong):
        return ApiError(
            ErrorCode.INVALID_REQUEST,
            f"That question, with what was found for it, is too long for the model "
            f"that was asked ({exc}). Ask something narrower, or raise OLLAMA_NUM_CTX "
            "for the local model. Nothing fell back to another model.",
        )
    # Still no automatic fallback, in either direction. A local model that
    # is down must not silently promote the question to a cloud provider —
    # that would send the context off the machine without anyone choosing
    # it, which is the one thing the per-request switch exists to prevent.
    # A remote provider that fails does not quietly drop to the local one
    # either: the answer would come from a different model than the one
    # asked for, and `LLM_FALLBACK_LOCAL` deliberately does not reach here.
    if selected == LOCAL_PROVIDER:
        return ApiError(
            ErrorCode.INTERNAL_ERROR,
            f"The local model is not answering ({exc}). {_local_remedy(exc)} "
            "Nothing was sent anywhere else — pick a cloud provider explicitly "
            "if you want one.",
        )
    return ApiError(
        ErrorCode.INTERNAL_ERROR,
        f"{selected} did not answer ({exc}). Nothing fell back to another model — "
        "ask again, or switch to the local one.",
    )


def reply_for(ready: Ready, model: str | None, answer: str) -> ChatReply:
    """The finished answer and everything the dock lists under it."""
    selected, include_mail, found = ready.selected, ready.include_mail, ready.found
    cited = cited_labels(answer)
    return ChatReply(
        reply=answer,
        count_facts=list(count_facts(found)),
        provider=selected,
        model=model,
        grounded=True,
        # What actually happened, not what was asked for — the two differ for
        # the local model, which always sees the mail whatever the toggle says.
        shared_mail=include_mail,
        # Computed, never assumed from the provider name: `is_local` is what
        # knows that an Ollama-served `:cloud` model is not local.
        local=is_local(selected, model),
        sources=[
            ChatSource(
                label=passage.label,
                posting_id=passage.posting_id,
                title=passage.title,
                company=passage.company,
                location=passage.location,
                url=passage.url,
                cited=passage.label in cited,
                kind=passage.kind,
            )
            # What the search found for this question, and of the postings
            # carried over from earlier answers only the ones this one cites.
            for passage in (*found.passages, *ready.earlier)
            if passage in found.passages or passage.label in cited
        ],
        history_used=len(ready.remembered.turns),
        history_withheld=ready.remembered.withheld,
        mail_in_context=ready.mail_in_context,
        postings_work_mode="remote" if found.work_mode == "remote" else None,
        postings_searched=found.searched,
        postings_unsearchable=found.unsearchable,
        postings_reranked_by=found.reranked_by,
        more_sources=[
            ChatSource(
                label="",
                posting_id=listed.posting_id,
                title=listed.title,
                company=listed.company,
                location=listed.location,
                url=listed.url,
                kind=listed.kind,
            )
            for listed in found.more
        ],
        matched_kinds=[ChatKind(label=kind, count=number) for kind, number in found.kinds],
        related_roles=[
            ChatRelatedRole(label=display_name(key), filter=key) for key in found.related
        ],
        matched_role=" or ".join(found.roles) or None,
        postings_matched_total=(
            found.role_total
            if found.role_total is not None
            else found.title_total
            if found.title_total is not None
            else found.match_total
        ),
        companies_matched_total=found.company_total,
        postings_share=ChatPostingShare.model_validate(found.share) if found.share else None,
        matched_term=found.matched_term,
        matched_companies=list(found.companies) if found.match_total is not None else [],
        place_filter="Bay Area" if found.place_total is not None else None,
        place_matched_total=found.place_total,
        place_counts_complete=found.place_counts_complete,
        search_area_matched_total=found.search_area_total,
        remote_or_unplaced_not_listed=found.remote_or_unplaced_not_listed,
        matched_title_words=list(found.title_words),
        postings_related_total=found.related_total,
        matched_role_filter=found.role_keys[0] if len(found.role_keys) == 1 else None,
        # A posting carried over from an earlier answer was supplied too, under
        # a label of its own (`chat_history.earlier_postings`), so citing it is
        # not inventing one. The check knew only what was found for this
        # question, and reported "the one from before [P6]" as made up.
        unsupported_citations=unsupported_citations(
            cited, (p.label for p in (*found.passages, *ready.earlier))
        ),
        postings_open=found.searched + found.unsearchable,
        postings_ranked=found.ranked,
        postings_reranked=found.reranked,
        postings_supplied=len(found.passages) + len(ready.earlier),
        gap_postings_counted=ready.gap_counted,
    )


def fixed_reply(ready: Ready) -> ChatReply | None:
    """Same source metadata as model answers, truthful local/provider labels."""
    if ready.factual_answer is None:
        return None
    return reply_for(ready, None, ready.factual_answer).model_copy(
        update={
            "provider": "facts",
            "model": None,
            "local": True,
            "shared_mail": False,
            "mail_in_context": False,
        }
    )


async def selected_reply(ready: Ready) -> ChatReply:
    """Validate the complete selection before exposing any model-dependent text."""
    evidence = ready.selection_evidence
    assert evidence is not None
    try:
        provider, model = provider_for(ready.selected)
        raw = await provider.complete(SYSTEM, ready.prompt, max_tokens=180, temperature=0.0)
    except LLMError as exc:
        raise did_not_answer(ready.selected, exc) from exc
    return selection_reply_for(ready, model, raw)


def selection_reply_for(ready: Ready, model: str | None, raw: str) -> ChatReply:
    """Render captured selections through the same validation as live replies."""
    evidence = ready.selection_evidence
    assert evidence is not None
    selection = parse_selection(raw, evidence)
    if not selection.valid or not selection.items:
        lead = (
            "No answer to the requested task was established by this selection. "
            "Closest retrieved source excerpts follow."
        )
        selection = SelectionResult(items=evidence[:8])
    else:
        lead = (
            "Selected source excerpts follow. They do not establish an exhaustive list "
            "or independently verify the requested task."
        )
    answer = "\n\n".join(
        (
            ready.selection_summary or render_postings(ready.found, include_sources=False),
            lead,
            render_selection(selection),
        )
    )
    reply = reply_for(ready, model, answer)
    # Citation-looking text inside a source quote is source material. Only
    # validated catalogue labels may supply the response citation metadata.
    labels = set(selection.source_labels)
    return reply.model_copy(
        update={
            "unsupported_citations": [],
            "sources": [
                source.model_copy(update={"cited": source.label in labels})
                for source in reply.sources
            ],
        }
    )


@router.post("", response_model=ChatReply)
async def chat(body: ChatRequest, session: SessionDep) -> ChatReply:
    """Answer a question about the owner's own job search."""
    ready = await prepare(body, session)
    if isinstance(ready, ChatReply):
        return ready
    fixed = fixed_reply(ready)
    if fixed is not None:
        return fixed
    if ready.selection_evidence is not None:
        return await selected_reply(ready)
    try:
        provider, model = provider_for(ready.selected)
        answer = await provider.complete(
            SYSTEM, ready.prompt, max_tokens=ANSWER_TOKENS, temperature=ANSWER_TEMPERATURE
        )
    except LLMError as exc:
        raise did_not_answer(ready.selected, exc) from exc
    return reply_for(ready, model, answer)
