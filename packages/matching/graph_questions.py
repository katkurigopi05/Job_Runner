"""Complete graph questions with one known place, employer, role, or topic suffix.

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
_TOPIC = re.compile(r"(?:related to|about) (.+)", re.I)
#: Endings that make a topic carry a second constraint; the last "in", "at" or "for" counts.
_IN_PLACE = re.compile(r".*\bin\s+(.+)$", re.I)
_AT_COMPANY = re.compile(r".*\bat\s+(.+)$", re.I)
_FOR_ROLE = re.compile(r".*\bfor\s+(.+?)\s+roles?$", re.I)


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
    if _PROJECT_QUESTION.fullmatch(clean):
        return GraphQuestion("projects")
    if _GAP_QUESTION.fullmatch(clean):
        return GraphQuestion("gaps")
    base = _PROJECT_QUESTION.match(clean)
    if base is None:
        return None
    suffix = clean[base.end() :]
    constraint = None
    if suffix.startswith(" "):
        suffix = suffix.strip()
        if match := re.fullmatch(r"in (.+)", suffix, re.I):
            if place := locality.explicit_place_name(match[1]):
                label = "in the Bay Area" if place == "Bay Area" else f"in {place}"
                constraint = GraphConstraint(label, place=place)
        elif match := re.fullmatch(r"for (.+) roles?", suffix, re.I):
            if role := roles.exact_role(match[1]):
                constraint = GraphConstraint(f"for {roles.display_name(role)} roles", role=role)
        elif (
            (match := re.fullmatch(r"at (.+)", suffix, re.I))
            and session is not None
            and (company := await resolve_company_name(session, match[1]))
        ):
            constraint = GraphConstraint(f"at {company[1]}", company_ids=company[0])
        elif (match := _TOPIC.fullmatch(suffix)) and session is not None:
            topic = match[1].strip()
            if topic and not await _carries_another_constraint(session, topic):
                nearest = await _nearest_postings(session, topic)
                constraint = GraphConstraint(
                    f"related to {topic}", posting_ids=frozenset(nearest), ranked=tuple(nearest)
                )
    return GraphQuestion("projects", constraint) if constraint is not None else None


async def _carries_another_constraint(session: AsyncSession, topic: str) -> bool:
    """A topic ending in a known place, employer or role names a second constraint."""
    if (match := _IN_PLACE.search(topic)) and locality.explicit_place_name(match[1]):
        return True
    if (match := _FOR_ROLE.search(topic)) and roles.exact_role(match[1]):
        return True
    return bool(
        (match := _AT_COMPANY.search(topic)) and await resolve_company_name(session, match[1])
    )


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
