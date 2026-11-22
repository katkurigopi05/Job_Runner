"""Complete graph questions with a place, role, role and place, employer, or topic.

A topic ("related to RAG pipelines", "about retrieval systems") is found by
meaning: the topic is embedded on this machine with Qwen3-Embedding-4B and the
`TOPIC_NEAREST` open postings nearest to it become the constraint. A topic
that ends in a place, an employer or a role is not a topic question, because
that part would be searched by meaning and never enforced.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Posting
from packages.matching import chunk_search, locality, location_fields, roles
from packages.matching.query_vectors import QueryEmbeddingError, embed_question
from packages.matching.retrieve import resolve_company_name

#: Open postings nearest a topic that a topic question reads, nearest first.
TOPIC_NEAREST = 200
#: A topic is a few words. A longer suffix is not a topic question, which also
#: bounds what is embedded and what the checks below read.
TOPIC_MAX_CHARS = 200


class TopicUnavailable(chunk_search.QwenSearchUnavailable):
    """Qwen could not embed a topic, so the topic question is refused, not searched another way."""


GraphIntent = Literal["projects", "gaps"]
_PROJECT_QUESTION = re.compile(
    r"(?:which|what) (?:jobs|roles)(?: in my top matches)? match "
    r"(?:the )?(?:skills (?:demonstrated in|from|on) )?my projects"
    r"|how do my projects connect to my job matches",
    re.I,
)
_GAP_QUESTION = re.compile(
    r"which skill gaps recur across my (?:strongest|top) matches"
    r"|which skill gaps recur across (?:all )?(?:open )?jobs in my (?:search )?area"
    r"|(?:compared with my r[ée]sum[ée]s,? )?"
    r"which skills do my top matches ask for that i am missing",
    re.I,
)


@dataclass(frozen=True)
class GraphConstraint:
    label: str
    place: str | None = None
    company_ids: tuple[uuid.UUID, ...] = ()
    role: str | None = None
    #: The open postings nearest a topic, by meaning; None for no topic.
    posting_ids: frozenset[uuid.UUID] | None = None
    #: Those postings nearest first: a topic answer lists the most related jobs first.
    ranked: tuple[uuid.UUID, ...] = ()

    @property
    def waives_area(self) -> bool:
        return (
            self.place is not None
            and locality.locality_of(self.place) is locality.Locality.ELSEWHERE
        )

    def keeps(self, posting: Posting) -> bool:
        if self.posting_ids is not None and posting.id not in self.posting_ids:
            return False
        if self.company_ids and posting.company_id not in self.company_ids:
            return False
        if self.role and roles.canonical(posting.title or "") != self.role:
            return False
        if self.place is None:
            return True
        place = locality.locality_of(self.place)
        if place is locality.Locality.BAY_AREA:
            cities = locality.bay_area_city_names(self.place)
            return (
                locality.names_city_office(posting.location, title=posting.title, cities=cities)
                if cities
                else locality.names_bay_area_office(posting.location, title=posting.title)
            )
        if self.place == "California":
            return locality.names_california_office(posting.location, title=posting.title)
        if self.place == "United States":
            return any(
                locality.is_domestic(locality.locality_of(part))
                for part in locality.locations_in(posting.location)
            )
        country = location_fields.country_code(self.place)
        if country and self.place.lower() not in locality.CODE_BY_STATE:
            reading = location_fields.normalize(posting.location, title=posting.title)
            return any(office["country_code"] == country for office in reading["locations_json"])
        # Other explicit cities/states need that complete place in an office,
        # never a mention in a job description. As in the shared office readers,
        # the title supplies a place only when the location names none.
        location = posting.title if locality.names_no_place(posting.location) else posting.location
        return any(
            locality.locality_of(part) == place
            and re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", part, re.I)
            for part in locality.locations_in(location)
            for alias in locality.location_aliases(self.place)
        )


@dataclass(frozen=True)
class GraphQuestion:
    intent: GraphIntent
    constraint: GraphConstraint | None = None


async def graph_intent(
    question: str, *, session: AsyncSession | None = None
) -> GraphQuestion | None:
    clean = " ".join(question.strip().rstrip("?.!").split())
    questions: tuple[tuple[GraphIntent, re.Pattern[str]], ...] = (
        ("projects", _PROJECT_QUESTION),
        ("gaps", _GAP_QUESTION),
    )
    for intent, pattern in questions:
        if pattern.fullmatch(clean):
            return GraphQuestion(intent)
    for intent, pattern in questions:
        base = pattern.match(clean)
        if base is None:
            continue
        suffix = clean[base.end() :]
        constraint = await _constraint(suffix.strip(), session) if suffix.startswith(" ") else None
        return GraphQuestion(intent, constraint) if constraint is not None else None
    return None


def _after(suffix: str, *prefixes: str) -> str | None:
    """What follows the first of `prefixes` (any case) the suffix starts with, else None.

    Prefix and word checks, not patterns: the suffix is the owner's typed text,
    and CodeQL flags a pattern with a repeat over it (PRs #331, #333).
    """
    for prefix in prefixes:
        if suffix[: len(prefix)].lower() == prefix and len(suffix) > len(prefix):
            return suffix[len(prefix) :]
    return None


def _role_words(suffix: str) -> str | None:
    """The role named in "for data engineer roles" ("data engineer"), else None."""
    words = suffix.split()
    if len(words) > 2 and words[0].lower() == "for" and words[-1].lower() in {"role", "roles"}:
        return " ".join(words[1:-1])
    return None


def _place_label(place: str) -> str:
    return "in the Bay Area" if place == "Bay Area" else f"in {place}"


def _role_and_place(suffix: str) -> GraphConstraint | None:
    """Read both clauses in either order, requiring a complete role and place."""
    suffix = suffix.lower()
    if suffix.startswith("for "):
        role_clause, separator, where = suffix.rpartition(" in ")
    elif suffix.startswith("in "):
        where, separator, role_tail = suffix.removeprefix("in ").rpartition(" for ")
        role_clause = f"for {role_tail}"
    else:
        return None
    if (
        separator
        and (role_words := _role_words(role_clause))
        and (role := roles.exact_role(role_words))
        and (place := locality.explicit_place_name(where))
    ):
        return GraphConstraint(
            f"for {roles.display_name(role)} roles {_place_label(place)}", place=place, role=role
        )
    return None


async def _constraint(suffix: str, session: AsyncSession | None) -> GraphConstraint | None:
    """Read the entire suffix as one supported constraint or a role and place."""
    if compound := _role_and_place(suffix):
        return compound
    if (where := _after(suffix, "in ")) is not None:
        if place := locality.explicit_place_name(where):
            return GraphConstraint(_place_label(place), place=place)
    elif (role_words := _role_words(suffix)) is not None:
        if role := roles.exact_role(role_words):
            return GraphConstraint(f"for {roles.display_name(role)} roles", role=role)
    elif (
        (employer := _after(suffix, "at ")) is not None
        and session is not None
        and (company := await resolve_company_name(session, employer))
    ):
        return GraphConstraint(f"at {company[1]}", company_ids=company[0])
    elif (about := _after(suffix, "related to ", "about ")) is not None and session is not None:
        topic = about.strip()
        if (
            topic
            and len(topic) <= TOPIC_MAX_CHARS
            and not await _carries_another_constraint(session, topic)
        ):
            nearest = await _nearest_postings(session, topic)
            return GraphConstraint(
                f"related to {topic}", posting_ids=frozenset(nearest), ranked=tuple(nearest)
            )
    return None


def _after_last(words: list[str], word: str) -> list[str]:
    """The words after the last `word` (any case); [] when it is absent or last.

    A scan over the words, not a regex: the topic is the owner's typed text,
    and a backtracking pattern over it can take seconds (CodeQL, PR #331).
    """
    for index in range(len(words) - 2, -1, -1):
        if words[index].lower() == word:
            return words[index + 1 :]
    return []


async def _carries_another_constraint(session: AsyncSession, topic: str) -> bool:
    """A topic ending in a known place, employer or role names a second constraint."""
    words = topic.split()
    place = _after_last(words, "in")
    if place and locality.explicit_place_name(" ".join(place)):
        return True
    role = _after_last(words, "for")
    if (
        len(role) > 1
        and role[-1].lower() in {"role", "roles"}
        and roles.exact_role(" ".join(role[:-1]))
    ):
        return True
    company = _after_last(words, "at")
    return bool(company and await resolve_company_name(session, " ".join(company)))


async def _nearest_postings(session: AsyncSession, topic: str) -> list[uuid.UUID]:
    """The open postings nearest the topic by Qwen, nearest first.

    The topic is embedded here and the model unloaded at once, so a topic
    question never holds it beside a local chat model.
    """
    try:
        vector = await embed_question(topic, keep_alive="0")
    except QueryEmbeddingError as exc:
        raise TopicUnavailable(str(exc)) from exc
    ranking = chunk_search.ShortlistRanking(list(vector.values))
    rows, _ = await ranking.read(session, among=None, companies=None, limit=TOPIC_NEAREST)
    return [posting_id for posting_id, _, _ in rows[:TOPIC_NEAREST]]
