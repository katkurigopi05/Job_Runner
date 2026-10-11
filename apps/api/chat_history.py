"""What the assistant is given of the conversation so far.

`/chat` took one message, so "which of those are remote?" referred to
nothing. The dashboard now sends the last few exchanges with the question.
That is the client's copy of earlier answers, and three of the assistant's
rules were written for one message at a time:

- **Recruiter mail (§14).** The local model always reads it, so its answer
  can quote it. Passed on as history, that is the mail reaching a remote
  model with the box unticked. An exchange written with mail in its context
  goes to a remote model only when the owner shares mail for this question.
- **§2.2.** "What salary does this posting list?" is answered and "what
  should I ask for?" names no protected topic. Together they are the
  question the rule refuses. A first-person question is asked without any
  earlier exchange that touched one of those topics.
- **Grounded, not freehand (§14).** An earlier answer is the model's own
  prose. The postings it cited are read again from the database and given as
  context; what it said of them is not.

Whatever is held back is said in the prompt, as the withheld mail is: an
absent exchange would read as nothing having been said.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import protected_questions as protected
from packages.core.models import Company, Posting
from packages.core.schemas import ChatTurn
from packages.matching.retrieve import Passage

#: How many exchanges the model is given, newest last. The local model reads
#: 4,096 tokens and refuses a prompt that does not fit; the longest prompt
#: without history is about 1,400 with 600 to answer. Three exchanges at the
#: lengths below add about 600.
MAX_TURNS = 3
QUESTION_CHARS = 300
ANSWER_CHARS = 500
#: How many postings from earlier answers are read again.
EARLIER_POSTINGS = 6

HEADING = "EARLIER IN THIS CONVERSATION"
POSTINGS_HEADING = "POSTINGS FROM EARLIER ANSWERS"

#: A citation as a model writes one: [P1], 【P2】, (P3, P4). In an earlier
#: answer it named a posting found for that question. Here the same label
#: names another, and a model that repeats it marks the wrong one as cited.
_CITATION = re.compile(r"\s*(?:\[|【|\()\s*P\d+(?:\s*,\s*P\d+)*\s*(?:\]|】|\))")
_LIST_CITATION = re.compile(
    r"^([ \t]*(?:[-*•]|\d+[.)])?[ \t]*)\**P\d+\**[ \t]*[:–—-][ \t]*", re.MULTILINE
)


@dataclass(frozen=True)
class Remembered:
    """The history, sorted into what the model sees and what it does not."""

    turns: tuple[ChatTurn, ...] = ()
    held_for_mail: int = 0
    held_for_topic: int = 0
    #: Postings the recent answers cited, newest answer first.
    posting_ids: tuple[uuid.UUID, ...] = ()

    @property
    def withheld(self) -> int:
        return self.held_for_mail + self.held_for_topic


def remember(history: Sequence[ChatTurn], *, question: str, mail_may_go: bool) -> Remembered:
    """Decide what of the conversation goes with this question.

    `mail_may_go` is what `/chat` already worked out for the context itself:
    true for the local model, and for a remote one only when the owner shared
    mail for this question.
    """
    recent = history[-MAX_TURNS:]
    about_themselves = protected.is_first_person(question)
    kept: list[ChatTurn] = []
    held_for_mail = held_for_topic = 0
    cited: list[uuid.UUID] = []
    for turn in recent:
        # A question the rule refuses was never answered by a model. One that
        # arrives here anyway is not handed on with an answer beside it.
        if protected.is_refused(turn.question):
            held_for_topic += 1
            continue
        # Which postings were cited is not mail and not an answer for the
        # profile, so it is kept whatever happens to the words.
        cited = [*turn.cited, *cited]
        if about_themselves and (
            protected.touches_a_protected_topic(turn.question)
            or protected.touches_a_protected_topic(turn.answer)
        ):
            held_for_topic += 1
        elif turn.mail_in_context and not mail_may_go:
            held_for_mail += 1
        else:
            kept.append(turn)
    return Remembered(
        turns=tuple(kept),
        held_for_mail=held_for_mail,
        held_for_topic=held_for_topic,
        posting_ids=tuple(dict.fromkeys(cited))[:EARLIER_POSTINGS],
    )


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _without_citations(answer: str) -> str:
    return _CITATION.sub("", _LIST_CITATION.sub(r"\1", answer))


def section(remembered: Remembered) -> str:
    """The conversation as the prompt carries it. Empty for a first question."""
    if not remembered.turns and not remembered.withheld:
        return ""
    lines = [
        f"{HEADING} (what was asked and what you answered, so you know what the "
        "question refers to; not a source of facts):"
    ]
    for turn in remembered.turns:
        lines.append(f"  the owner asked: {_clip(turn.question, QUESTION_CHARS)}")
        lines.append(f"  you answered: {_clip(_without_citations(turn.answer), ANSWER_CHARS)}")
    if remembered.held_for_mail:
        lines.append(
            f"  {remembered.held_for_mail} earlier exchange(s) withheld: recruiter mail was "
            "in the context, and it is not shared with a remote model"
        )
    if remembered.held_for_topic:
        lines.append(
            f"  {remembered.held_for_topic} earlier exchange(s) left out: about pay, "
            "sponsorship, work authorization or employment history, which come from the "
            "owner's profile and not from you"
        )
    return "\n".join(lines)


async def earlier_postings(
    session: AsyncSession,
    posting_ids: Sequence[uuid.UUID],
    *,
    found_now: Sequence[Passage],
) -> tuple[Passage, ...]:
    """The postings earlier answers cited, read again and labelled after `found_now`.

    One found again for this question is left to its label there. An id that
    is no posting any more is passed over.
    """
    already = {passage.posting_id for passage in found_now}
    wanted = [posting_id for posting_id in posting_ids if posting_id not in already]
    if not wanted:
        return ()
    rows = (
        await session.execute(
            select(Posting, Company.name)
            .outerjoin(Company, Company.id == Posting.company_id)
            .where(Posting.id.in_(wanted))
        )
    ).all()
    by_id = {posting.id: (posting, company) for posting, company in rows}
    read = [by_id[posting_id] for posting_id in wanted if posting_id in by_id]
    return tuple(
        Passage(
            label=f"P{number}",
            posting_id=posting.id,
            title=(posting.title or "an untitled posting")
            + (" (closed)" if posting.closed_at is not None else ""),
            company=company,
            location=posting.location,
            url=posting.url,
            excerpt="",
            application_status=None,
        )
        for number, (posting, company) in enumerate(read, start=len(found_now) + 1)
    )


def postings_section(earlier: Sequence[Passage]) -> str:
    lines = [f"{POSTINGS_HEADING} (read again now; cite them by label like any other):"]
    for passage in earlier:
        where = " — ".join(part for part in (passage.company, passage.location) if part)
        lines.append(f"  [{passage.label}] {passage.title}" + (f" — {where}" if where else ""))
    return "\n".join(lines)
