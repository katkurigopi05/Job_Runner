"""Source-backed graph retrieval for personal project matches and recurring gaps.

The graph is a request-local projection of PostgreSQL facts, not a second index.
Edges retain their source reference. No model extracts edges or infers proficiency.
Only skill labels, anonymous project references and public posting evidence enter
the selection prompt; local rendering restores the project's source attribution.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from packages.core.config import get_settings
from packages.core.models import Candidate, Company, Match, Posting, Profile, Project, Resume
from packages.core.models_skill_links import PostingSkill, PostingSkillRead
from packages.matching import skill_links
from packages.matching.evidence import Strength, skill_evidence
from packages.matching.gaps import resume_skills
from packages.matching.graph_questions import GraphConstraint, GraphIntent, GraphQuestion
from packages.matching.graph_questions import graph_intent as graph_intent
from packages.matching.locality import area_exclusion
from packages.matching.location_fields import office_facts
from packages.matching.required_skills import fresh_requirements
from packages.matching.retrieve import Passage, Retrieval
from packages.matching.skill_vocab import BY_KEY, normalize_skill

TARGET_LIMIT = 100
#: Postings read per query when the saved links widen the search to every open job.
LOAD_BATCH = 5000
RESULT_LIMIT = 8
PROJECT_RESULT_LIMIT = 5
#: What a graph answer read: a sample of the owner's best matches, or every open
#: posting in the search area through the saved links (`skill_links.py`).
GraphScope = Literal["top_matches", "open_postings"]


@dataclass(frozen=True)
class Entity:
    kind: str
    key: str


@dataclass(frozen=True)
class Edge:
    source: Entity
    relation: str
    target: Entity
    reference: str
    quote: str = ""


@dataclass
class KnowledgeGraph:
    outgoing: dict[Entity, list[Edge]] = field(default_factory=lambda: defaultdict(list))
    incoming: dict[Entity, list[Edge]] = field(default_factory=lambda: defaultdict(list))

    def add(self, edge: Edge) -> None:
        self.outgoing[edge.source].append(edge)
        self.incoming[edge.target].append(edge)

    def links(self, entity: Entity, relation: str, *, incoming: bool = False) -> list[Edge]:
        index = self.incoming if incoming else self.outgoing
        return [edge for edge in index.get(entity, ()) if edge.relation == relation]


@dataclass(frozen=True)
class GraphEvidence:
    label: str
    text: str
    model_text: str
    answer: str = ""


@dataclass(frozen=True)
class SavedLinks:
    """A posting's saved direct requirements, read from its current text (`skill_links.py`)."""

    readable: bool
    #: (skill key, the posting's quote), direct requirements only.
    required: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class GraphResult:
    found: Retrieval
    summary: str
    evidence: tuple[GraphEvidence, ...] = ()
    answer_summary: str = ""
    answer_note: str = ""
    scope: GraphScope = "top_matches"

    def answer(self, labels: Sequence[str]) -> str:
        by_label = {item.label: item for item in self.evidence}
        sections = [self.answer_summary]
        for label in labels:
            item = by_label[label]
            heading, _, body = (item.answer or _markdown(item.text)).partition("\n")
            sections.append(f"{heading} [{label}]" + (f"\n{body}" if body else ""))
        sections.append(self.answer_note)
        return "\n\n".join(section for section in sections if section)

    def prompt(self, question: str, labels: frozenset[str] | None = None) -> str:
        return (
            "EVIDENCE_SELECTION\nCONTEXT:\n"
            + self.summary
            + "\n"
            + json.dumps(
                {
                    "question": question,
                    "evidence": [
                        {"id": item.label, "source_label": item.label, "text": item.model_text}
                        for item in self.evidence
                        if labels is None or item.label in labels
                    ],
                },
                ensure_ascii=False,
            )
        )


def project_keys(project: Project) -> set[str]:
    """Skills a project's source declares: primary language and dependency files only."""
    names = [project.language or "", *(project.dependency_technologies or [])]
    return {normalized for name in names if (normalized := normalize_skill(name)) is not None}


def build_graph(
    candidate_id: uuid.UUID,
    resumes: Sequence[Resume],
    projects: Sequence[Project],
    postings: Sequence[tuple[Posting, str | None]],
    *,
    links: Mapping[uuid.UUID, SavedLinks] | None = None,
) -> KnowledgeGraph:
    """Build only declared relationships; descriptions never imply project skills."""
    graph = KnowledgeGraph()
    owner = Entity("candidate", str(candidate_id))
    for resume in resumes:
        if (
            resume.candidate_id != candidate_id
            or resume.tailored_for_posting_id is not None
            or resume.tailored_key is not None
        ):
            continue
        node = Entity("resume", str(resume.id))
        graph.add(Edge(owner, "has_resume", node, node.key))
        text = "\n".join((resume.parsed_json or {}).get("raw_lines") or [])
        for key in sorted(resume_skills(text)):
            graph.add(Edge(node, "lists", Entity("skill", key), node.key))
    for project in projects:
        if not is_eligible(project, candidate_id):
            continue
        node = Entity("project", str(project.id))
        graph.add(Edge(owner, "has_project", node, project.url))
        # Primary language and manifest declarations are source facts. Prose,
        # topics and repository names do not establish use of a technology.
        for key in sorted(project_keys(project)):
            graph.add(Edge(node, "declares", Entity("skill", key), project.url))
    for posting, _ in postings:
        node = Entity("posting", str(posting.id))
        if posting.company_id is not None:
            graph.add(
                Edge(node, "posted_by", Entity("company", str(posting.company_id)), posting.url)
            )
        saved = (links or {}).get(posting.id)
        if saved is not None:
            # Read and filtered when the posting was linked: no extraction here.
            for key, quote in saved.required:
                if key in BY_KEY:
                    graph.add(Edge(node, "requires", Entity("skill", key), posting.url, quote))
            continue
        requirements = fresh_requirements(
            posting.requirements_json, posting.requirements_version, posting.description_raw
        )
        keys = {
            entry.get("skill")
            for entry in ((requirements or {}).get("skills") or {}).get("required", [])
        }
        for key in sorted(key for key in keys if key in BY_KEY):
            evidence = skill_evidence(requirements, key)
            if (
                evidence is not None
                and evidence.strength is Strength.REQUIRED
                and not evidence.option
                and evidence.quote
            ):
                graph.add(Edge(node, "requires", Entity("skill", key), posting.url, evidence.quote))
    return graph


def graph_result(
    candidate_id: uuid.UUID,
    resumes: Sequence[Resume],
    projects: Sequence[Project],
    postings: Sequence[tuple[Posting, str | None]],
    intent: GraphIntent,
    *,
    links: Mapping[uuid.UUID, SavedLinks] | None = None,
    scope: GraphScope = "top_matches",
    unlinked: int = 0,
    constraint: GraphConstraint | None = None,
) -> GraphResult:
    graph = build_graph(candidate_id, resumes, projects, postings, links=links)
    owner = Entity("candidate", str(candidate_id))
    project_nodes = [e.target for e in graph.links(owner, "has_project")]
    project_by_id = {str(p.id): p for p in projects}
    project_skills: dict[Entity, list[Entity]] = defaultdict(list)
    for node in project_nodes:
        for edge in graph.links(node, "declares"):
            project_skills[edge.target].append(node)
    resume_nodes = [e.target for e in graph.links(owner, "has_resume")]
    listed = {edge.target for node in resume_nodes for edge in graph.links(node, "lists")}
    every = scope == "open_postings"
    found_what = (
        "directly require a technology your projects declare"
        if intent == "projects"
        else "state at least one direct requirement"
    )
    reach = (
        "Graph retrieval read the saved requirements of every open, unskipped posting in the "
        f"configured search area and found {len(postings)} that {found_what}. "
        + (
            f"{unlinked} open postings have no saved requirements yet and were not read. "
            if unlinked
            else ""
        )
        if every
        else f"Graph retrieval examined {len(postings)} of your highest-scoring open, unskipped "
        "matches in the configured search area (at most 100; each job counted once). "
        "This is a bounded sample, not all available jobs. "
    )
    summary = (
        reach + "Only direct required skills count; "
        "preferences, alternatives and stack mentions are excluded. "
        "A shared technology does not establish proficiency or full job eligibility."
    )
    saved = links or {}
    read = sum(
        saved[p.id].readable
        if p.id in saved
        else fresh_requirements(p.requirements_json, p.requirements_version, p.description_raw)
        is not None
        for p, _ in postings
    )
    summary += f" Requirements readable: {read}; unread: {len(postings) - read}."
    passages: list[Passage] = []
    items: list[GraphEvidence] = []
    answer_summary = ""

    if intent == "projects":
        if not project_skills:
            summary += (
                " No eligible project technology declarations are available. Sync public "
                "GitHub projects with a primary language or dependency declarations first."
            )
        candidates = []
        for rank, (posting, company) in enumerate(postings):
            demands = graph.links(Entity("posting", str(posting.id)), "requires")
            shared = [edge for edge in demands if edge.target in project_skills]
            if shared:
                candidates.append((posting, company, shared, rank))
        if constraint is not None and constraint.ranked:
            # A topic question lists the jobs most related to the topic first.
            nearness = {posting_id: rank for rank, posting_id in enumerate(constraint.ranked)}
            candidates.sort(key=lambda item: nearness.get(item[0].id, len(nearness)))
        else:
            candidates.sort(key=lambda item: (-len(item[2]), item[3]))
        summary += (
            f" {len(candidates)} {'open' if every else 'sampled'} jobs share a directly "
            "required project technology."
        )
        summary += " Each result shows up to five shared technologies and three projects per skill."
        answer_summary = (
            "These jobs share technologies with your projects:"
            if candidates
            else "No open job in your search area directly requires a technology your "
            "projects declare."
            if every
            else f"No direct job requirements matched your project technologies in the "
            f"{len(postings)} top matches reviewed."
        )
        if not project_skills:
            answer_summary = (
                "No eligible project technology declarations are available. "
                "Sync your public GitHub projects before comparing job requirements."
            )
        for posting, company, shared, _ in candidates[:PROJECT_RESULT_LIMIT]:
            local, model = [], []
            for edge in shared[:5]:
                skill_name = BY_KEY[edge.target.key].label
                nodes = project_skills[edge.target][:3]
                names = ", ".join(
                    f"{project_by_id[n.key].name} ({project_by_id[n.key].url})" for n in nodes
                )
                aliases = ", ".join(f"project {project_nodes.index(n) + 1}" for n in nodes)
                suffix = f' → {skill_name} ← direct job requirement: "{edge.quote}"'
                local.append(f"Repository declaration ({names}){suffix}")
                model.append(f"Repository declaration ({aliases}){suffix}")
            project_support: dict[Entity, list[str]] = defaultdict(list)
            for edge in shared[:5]:
                for node in project_skills[edge.target][:3]:
                    project_support[node].append(BY_KEY[edge.target.key].label)
            support = "; ".join(
                f"**{_markdown(project_by_id[node.key].name)}** ({', '.join(skills)})"
                for node, skills in project_support.items()
            )
            matched = ", ".join(BY_KEY[edge.target.key].label for edge in shared[:5])
            answer = (
                f"**{_markdown(posting.title or '(untitled)')} — "
                f"{_markdown(company or '(company not recorded)')}**"
                + (f" · {_markdown(posting.location)}" if posting.location else "")
                + f"\n   Matches: **{matched}**.\n   Project evidence: {support}."
            )
            missing = [
                BY_KEY[edge.target.key].label
                for edge in graph.links(Entity("posting", str(posting.id)), "requires")
                if edge.target not in project_skills
            ]
            if missing:
                answer += (
                    "\n   Also required, without evidence in these projects: "
                    + ", ".join(missing[:3])
                    + (f" and {len(missing) - 3} more" if len(missing) > 3 else "")
                    + "."
                )
            _append_evidence(
                passages, items, posting, company, "; ".join(local), "; ".join(model), answer=answer
            )
    else:
        if not resume_nodes or not listed:
            summary += " No readable base résumé skills are available; upload a base résumé first."
            answer_summary = (
                "Upload a readable base résumé to compare recurring skill requirements."
            )
        else:
            gaps = [
                (skill, graph.links(skill, "requires", incoming=True))
                for skill in graph.incoming
                if skill.kind == "skill" and skill not in listed
            ]
            gaps = [(skill, edges) for skill, edges in gaps if len(edges) >= 2]
            gaps.sort(key=lambda item: (-len(item[1]), item[0].key))
            where = "across open jobs in your area" if every else "in your top matches"
            answer_summary = (
                f"These skills are directly required {where} but are not listed on your "
                "base résumés:"
                if gaps
                else f"No recurring résumé skill gaps were found {where}."
            )
            summary += (
                f" {len(gaps)} skills absent from base résumé skill lists recur in at least "
                f"two {'open' if every else 'sampled'} jobs. "
                "Absence from a résumé is not proof you lack a skill."
            )
            jobs_read = (
                "open jobs in your area that state a direct requirement"
                if every
                else "sampled jobs"
            )
            by_id = {str(posting.id): (posting, company) for posting, company in postings}
            for skill, edges in gaps[:RESULT_LIMIT]:
                posting, company = by_id[edges[0].source.key]
                text = (
                    f"{BY_KEY[skill.key].label}: directly required by {len(edges)} of "
                    f"{len(postings)} {jobs_read}; absent from base résumé skill lists. "
                    + (
                        "A public project declares this technology; keep that evidence attributed "
                        "to its project. "
                        if skill in project_skills
                        else "No eligible public project declaration connects this skill. "
                    )
                    + f'Example job requirement: "{edges[0].quote}"'
                )
                human = (
                    f"**{BY_KEY[skill.key].label}** — directly required by {len(edges)} of "
                    f"{len(postings)} {'open jobs in your area' if every else 'reviewed jobs'}. "
                    + (
                        "An available project also declares this technology."
                        if skill in project_skills
                        else "No project declaration was found in this search."
                    )
                )
                _append_evidence(passages, items, posting, company, text, text, answer=human)
    population = "in your search area"
    if constraint is not None:
        population = constraint.label
        if not constraint.waives_area and get_settings().search_us_only:
            population += " within your search area"
        scope_note = f"Project search {constraint.label}: {len(postings)} jobs reviewed. "
        if constraint.waives_area:
            scope_note += "Explicit place; standing search area waived. "
        summary = scope_note + summary.replace("in the configured search area", population)
        answer_summary = f"Project matches {constraint.label}.\n\n" + answer_summary.replace(
            "in your search area", population
        )
        if not postings and project_skills:
            answer_summary = f"No open project matches found {constraint.label}."
    return GraphResult(
        found=Retrieval(
            attempted=True, passages=tuple(passages), searched=len(postings), ranked=len(items)
        ),
        summary=summary,
        evidence=tuple(items),
        answer_summary=answer_summary,
        scope=scope,
        answer_note=(
            (
                f"Based on all {len(postings)} open jobs {population} that "
                + (
                    "directly require one of your project technologies. "
                    if intent == "projects"
                    else "state a direct requirement. "
                )
                + (
                    f"{unlinked} open jobs are not linked yet (`make skill-links`). "
                    if unlinked
                    else ""
                )
                if every
                else f"Based on {len(postings)} of your top open job matches"
                + (f" {population}" if constraint else "")
                + ". "
            )
            + (
                f"Requirements were unreadable for {len(postings) - read} jobs. "
                if read < len(postings)
                else ""
            )
            + ("A résumé omission does not mean you lack a skill." if intent == "gaps" else "")
        ).strip(),
    )


def _markdown(text: str) -> str:
    # ReplyText supports bold, not Markdown escapes or links. Preserve ordinary
    # punctuation and prevent source text from introducing nested emphasis.
    return " ".join(text.split()).replace("**", "")


def _append_evidence(
    passages: list[Passage],
    items: list[GraphEvidence],
    posting: Posting,
    company: str | None,
    text: str,
    model_text: str,
    *,
    answer: str = "",
) -> None:
    label = f"P{len(passages) + 1}"
    prefix = f"{posting.title or '(untitled)'} at {company or '(company not recorded)'}: "
    passages.append(
        Passage(
            label,
            posting.id,
            posting.title or "(untitled)",
            company,
            posting.location,
            posting.url,
            model_text,
            None,
        )
    )
    items.append(GraphEvidence(label, prefix + text, prefix + model_text, answer))


async def retrieve_graph(
    session: AsyncSession,
    intent: GraphIntent | GraphQuestion,
    *,
    live_projects: Sequence[Project] | None = None,
) -> GraphResult:
    """Read current facts without indexing, migrations, provider calls or writes."""
    constraint = intent.constraint if isinstance(intent, GraphQuestion) else None
    intent = intent.intent if isinstance(intent, GraphQuestion) else intent
    candidates = list((await session.scalars(select(Candidate.id).limit(2))).all())
    if len(candidates) != 1:
        return GraphResult(
            Retrieval(attempted=False),
            "Graph retrieval needs exactly one candidate to avoid mixing personal evidence. "
            "Create a candidate first."
            if not candidates
            else (
                "Graph retrieval cannot choose among multiple candidates; "
                "no personal evidence was read."
            ),
        )
    candidate_id = candidates[0]
    resumes = list(
        (
            await session.scalars(
                select(Resume)
                .where(
                    Resume.candidate_id == candidate_id,
                    Resume.tailored_for_posting_id.is_(None),
                    Resume.tailored_key.is_(None),
                )
                .order_by(Resume.version)
            )
        ).all()
    )
    projects = list(
        (
            await session.scalars(
                select(Project)
                .where(
                    Project.candidate_id == candidate_id,
                    Project.source == "github",
                    Project.is_fork.is_(False),
                    Project.is_private.is_(False),
                    or_(Project.include.is_(None), Project.include.is_(True)),
                )
                .order_by(Project.id)
            )
        ).all()
    )
    if live_projects is not None:
        projects = list(live_projects)
    wide = (
        await _project_jobs_from_saved_links(session, candidate_id, projects, constraint)
        if intent == "projects"
        else await _gap_jobs_from_saved_links(session, candidate_id)
    )
    if wide is not None:
        postings, links, unlinked = wide
        return graph_result(
            candidate_id,
            resumes,
            projects,
            postings,
            intent,
            links=links,
            scope="open_postings",
            unlinked=unlinked,
            constraint=constraint,
        )
    best = (
        select(Match.posting_id, func.max(Match.score).label("score"))
        .join(Profile, Profile.id == Match.profile_id)
        .where(
            Profile.candidate_id == candidate_id,
            or_(Match.decision.is_(None), Match.decision != "skipped"),
        )
        .group_by(Match.posting_id)
        .subquery()
    )
    rows = await session.stream(
        select(Posting, Company.name)
        .join(best, best.c.posting_id == Posting.id)
        .outerjoin(Company, Company.id == Posting.company_id)
        .where(Posting.closed_at.is_(None))
        .order_by(best.c.score.desc(), Posting.id)
        .execution_options(yield_per=100)
    )
    kept: list[tuple[Posting, str | None]] = []
    try:
        async for posting, company in rows:
            if not _in_scope(posting, constraint):
                continue
            kept.append((posting, company))
            if len(kept) == TARGET_LIMIT:
                break
    finally:
        await rows.close()
    links = await saved_links(session, [posting.id for posting, _ in kept])
    return graph_result(
        candidate_id, resumes, projects, kept, intent, links=links, constraint=constraint
    )


def _in_scope(posting: Posting, constraint: GraphConstraint | None) -> bool:
    if constraint is not None and not constraint.keeps(posting):
        return False
    return bool(constraint and constraint.waives_area) or not _outside_area(posting)


def _outside_area(posting: Posting) -> bool:
    """The feed's search-area rule (`locality.area_exclusion`), when it is on."""
    return _area_reason(posting, posting.description_raw) is not None


def _area_reason(posting: Posting, description: str | None) -> str | None:
    """Why the search area leaves a posting out, or None to keep it."""
    settings = get_settings()
    if not settings.search_us_only:
        return None
    return area_exclusion(
        posting.location,
        title=posting.title,
        description=description,
        remote_outside_california=settings.search_remote_outside_california,
        country_code=posting.country_code,
        state_code=posting.state_code,
        is_remote=posting.is_remote,
        offices=office_facts(posting.locations_json),
    )


#: A posting's description only ever admits it, as remote; so only postings the
#: location fields leave out as on-site elsewhere are read again with it, the
#: rule `retrieve._matching_totals` follows.
_ONSITE_ELSEWHERE = " is on-site outside California"


async def _gap_jobs_from_saved_links(
    session: AsyncSession, candidate_id: uuid.UUID
) -> tuple[list[tuple[Posting, str | None]], dict[uuid.UUID, SavedLinks], int] | None:
    """Every open, unskipped posting in the area that states a direct requirement.

    Read from the saved links over all open postings rather than a sample of the
    owner's matches. None until a posting has been linked (`make skill-links`).
    Descriptions are not loaded except to admit an on-site posting as remote.
    """
    current_open = and_(Posting.closed_at.is_(None), skill_links.is_current())
    linked = await session.scalar(
        select(func.count())
        .select_from(PostingSkillRead)
        .join(Posting, Posting.id == PostingSkillRead.posting_id)
        .where(current_open)
    )
    if not linked:
        return None
    skipped = (
        select(Match.posting_id)
        .join(Profile, Profile.id == Match.profile_id)
        .where(Profile.candidate_id == candidate_id, Match.decision == "skipped")
    )
    ids = list(
        (
            await session.scalars(
                select(PostingSkill.posting_id)
                .join(Posting, Posting.id == PostingSkill.posting_id)
                .join(PostingSkillRead, PostingSkillRead.posting_id == Posting.id)
                .where(
                    current_open,
                    skill_links.is_direct_requirement(),
                    Posting.id.not_in(skipped),
                )
                .distinct()
            )
        ).all()
    )
    kept: list[tuple[Posting, str | None]] = []
    recheck: list[tuple[Posting, str | None]] = []
    for first in range(0, len(ids), LOAD_BATCH):
        rows = await session.execute(
            select(Posting, Company.name)
            .outerjoin(Company, Company.id == Posting.company_id)
            .options(
                defer(Posting.description_raw, raiseload=True),
                defer(Posting.description_embedding, raiseload=True),
            )
            .where(Posting.id.in_(ids[first : first + LOAD_BATCH]))
        )
        for posting, company in rows:
            reason = _area_reason(posting, None)
            if reason is None:
                kept.append((posting, company))
            elif reason.endswith(_ONSITE_ELSEWHERE):
                recheck.append((posting, company))
    for first in range(0, len(recheck), LOAD_BATCH):
        batch = recheck[first : first + LOAD_BATCH]
        rows = await session.execute(
            select(Posting.id, Posting.description_raw).where(
                Posting.id.in_([posting.id for posting, _ in batch])
            )
        )
        bodies: dict[uuid.UUID, str | None] = {row[0]: row[1] for row in rows}
        kept.extend(
            (posting, company)
            for posting, company in batch
            if _area_reason(posting, bodies.get(posting.id)) is None
        )
    kept.sort(
        key=lambda row: (
            -(row[0].first_seen_at.timestamp() if row[0].first_seen_at else 0.0),
            str(row[0].id),
        )
    )
    open_total = await session.scalar(
        select(func.count()).select_from(Posting).where(Posting.closed_at.is_(None))
    )
    links = await saved_links(session, [posting.id for posting, _ in kept])
    return kept, links, max(int(open_total or 0) - int(linked), 0)


async def saved_links(
    session: AsyncSession, posting_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, SavedLinks]:
    """Saved direct requirements of postings linked from their current text.

    A posting with no current link row is left out, and the graph reads it as
    before. Never writes.
    """
    found: dict[uuid.UUID, list[tuple[str, str]]] = {}
    readable: dict[uuid.UUID, bool] = {}
    for first in range(0, len(posting_ids), LOAD_BATCH):
        ids = list(posting_ids[first : first + LOAD_BATCH])
        reads = await session.execute(
            select(PostingSkillRead.posting_id, PostingSkillRead.readable)
            .join(Posting, Posting.id == PostingSkillRead.posting_id)
            .where(PostingSkillRead.posting_id.in_(ids), skill_links.is_current())
        )
        for posting_id, is_readable in reads:
            readable[posting_id] = is_readable
            found[posting_id] = []
        rows = await session.execute(
            select(PostingSkill.posting_id, PostingSkill.skill, PostingSkill.quote)
            .where(
                PostingSkill.posting_id.in_([i for i in ids if i in readable]),
                skill_links.is_direct_requirement(),
            )
            .order_by(PostingSkill.posting_id, PostingSkill.skill)
        )
        for posting_id, skill, quote in rows:
            found[posting_id].append((skill, quote))
    return {
        posting_id: SavedLinks(readable[posting_id], tuple(required))
        for posting_id, required in found.items()
    }


async def _project_jobs_from_saved_links(
    session: AsyncSession,
    candidate_id: uuid.UUID,
    projects: Sequence[Project],
    constraint: GraphConstraint | None = None,
) -> tuple[list[tuple[Posting, str | None]], dict[uuid.UUID, SavedLinks], int] | None:
    """Every open, unskipped posting in the area directly requiring a project technology.

    Read from the saved links over all open postings rather than a sample of the
    owner's matches. None when there is nothing to read them with: no project
    declares a technology, or no posting has been linked yet (`make skill-links`).
    """
    keys = sorted(
        {
            key
            for project in projects
            if is_eligible(project, candidate_id)
            for key in project_keys(project)
        }
    )
    if not keys:
        return None
    current_open = and_(Posting.closed_at.is_(None), skill_links.is_current())
    linked = await session.scalar(
        select(func.count())
        .select_from(PostingSkillRead)
        .join(Posting, Posting.id == PostingSkillRead.posting_id)
        .where(current_open)
    )
    if not linked:
        return None
    skipped = (
        select(Match.posting_id)
        .join(Profile, Profile.id == Match.profile_id)
        .where(Profile.candidate_id == candidate_id, Match.decision == "skipped")
    )
    counted = (
        await session.execute(
            select(PostingSkill.posting_id, func.count())
            .join(Posting, Posting.id == PostingSkill.posting_id)
            .join(PostingSkillRead, PostingSkillRead.posting_id == Posting.id)
            .where(
                current_open,
                skill_links.is_direct_requirement(),
                PostingSkill.skill.in_(keys),
                Posting.id.not_in(skipped),
            )
            .group_by(PostingSkill.posting_id)
        )
    ).all()
    shared: dict[uuid.UUID, int] = {row[0]: int(row[1]) for row in counted}
    kept: list[tuple[Posting, str | None]] = []
    ids = list(shared)
    for first in range(0, len(ids), LOAD_BATCH):
        rows = await session.execute(
            select(Posting, Company.name)
            .outerjoin(Company, Company.id == Posting.company_id)
            .where(Posting.id.in_(ids[first : first + LOAD_BATCH]))
        )
        kept.extend(
            (posting, company) for posting, company in rows if _in_scope(posting, constraint)
        )
    # Most shared technologies first, then the newest.
    kept.sort(
        key=lambda row: (
            -shared[row[0].id],
            -(row[0].first_seen_at.timestamp() if row[0].first_seen_at else 0.0),
            str(row[0].id),
        )
    )
    open_total = await session.scalar(
        select(func.count()).select_from(Posting).where(Posting.closed_at.is_(None))
    )
    links = await saved_links(session, [posting.id for posting, _ in kept])
    return kept, links, max(int(open_total or 0) - int(linked), 0)


def is_eligible(project: Project, candidate_id: uuid.UUID) -> bool:
    """The projects `build_graph` connects: the owner's public, included, non-fork GitHub ones."""
    return (
        project.candidate_id == candidate_id
        and project.source == "github"
        and not project.is_fork
        and not project.is_private
        and project.include is not False
    )
