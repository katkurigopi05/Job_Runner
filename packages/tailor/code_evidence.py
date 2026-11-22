"""Connect job requirements to reviewed résumé sources and live GitHub evidence."""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence

from pydantic import BaseModel, Field

from packages.core.models import Project
from packages.github.code_search import CodeFinding, SearchResult, search_code
from packages.matching.experience import Demand, heading_kind, read_posting
from packages.matching.requirements import Requirement, SkillMention, extract
from packages.matching.skill_vocab import CODE_SKILLS, find_skills
from packages.tailor.guard import SourceCorpus, entry_key, project_names
from packages.tailor.parse import ParsedResume

_RESPONSIBILITIES = re.compile(
    r"^(?:(?:technical |key |job )?responsibilities|what (?:you.ll|you will) do"
    r"|your (?:role|responsibilities)|the role):?$",
    re.I,
)


class RequirementMatch(BaseModel):
    skill: str
    label: str
    demand: Requirement
    quote: str
    option: bool
    resume_sources: list[str]
    finding_ids: list[str]
    status: str


class PostingContext(BaseModel):
    quote: str
    kind: str
    demand: str | None = None
    related_skills: list[str] = Field(default_factory=list)


class ClaimSuggestion(BaseModel):
    finding_id: str
    project_name: str
    # None means the imported project selected for this draft.
    entry: str | None
    wording: tuple[str, ...]


class CodeReview(BaseModel):
    requirements: list[RequirementMatch]
    context: list[PostingContext]
    findings: list[CodeFinding]
    limitations: list[str]
    suggestions: list[ClaimSuggestion] = Field(default_factory=list)
    repositories_checked: int


def claim_destination(
    project: Project, chosen: set[uuid.UUID], resume: ParsedResume
) -> tuple[str, str | None] | None:
    """Resolve a selected repository or the exact base Projects entry the owner linked."""
    if project.id in chosen:
        return project.name, None
    if project.resume_entry:
        matches = [
            name
            for name in project_names(resume)
            if entry_key(name) == entry_key(project.resume_entry)
        ]
        if len(matches) == 1:
            return matches[0], matches[0]
    return None


def add_linked_claim(
    entry: str, claim: str, base: ParsedResume, sections: dict[str, list[str]]
) -> bool:
    """Insert after the original title, under the named Projects entry only."""
    if entry not in project_names(base):
        return False
    lines = sections.get("projects", [])
    indexes = [index for index, line in enumerate(lines) if entry_key(line) == entry_key(entry)]
    if len(indexes) != 1:
        return False
    # Require the original title, not an edited line that happened to retain its name.
    titles = [line for line in base.section("projects") if entry_key(line) == entry_key(entry)]
    index = indexes[0]
    if titles != [lines[index]]:
        return False
    lines.insert(index + 1, f"• {claim}")
    return True


def extract_code_requirements(job_description: str) -> list[SkillMention]:
    """Order extracted code skills identically for Studio and the LangGraph pilot."""
    return sorted(
        extract(job_description, vocabulary=CODE_SKILLS).skills,
        key=lambda need: (
            need.requirement is not Requirement.REQUIRED or need.option,
            need.requirement is Requirement.UNCLASSIFIED,
            need.skill not in {"aws_lambda", "fastapi", "kubernetes", "docker", "cicd"},
        ),
    )


async def retrieve_evidence(job_description: str, projects: Sequence[Project]) -> SearchResult:
    return await search_code(projects, extract_code_requirements(job_description))


def review_evidence(
    job_description: str,
    resume: ParsedResume,
    result: SearchResult,
    *,
    projects: Sequence[Project] = (),
    chosen: set[uuid.UUID] | None = None,
    draft_resume: ParsedResume | None = None,
) -> CodeReview:
    posting = extract(job_description, vocabulary=CODE_SKILLS)
    sources: dict[str, list[str]] = {}
    corpus = SourceCorpus.from_resume(resume)
    for text, label in [
        *((item.text, item.ref) for item in corpus.items),
        *(
            (line, section)
            for section, lines in resume.sections.items()
            if section not in {"experience", "projects"}
            for line in lines
        ),
    ]:
        for skill in find_skills(text, CODE_SKILLS):
            sources.setdefault(skill.key, []).append(label)

    requirements = []
    for need in posting.skills:
        found = [finding for finding in result.findings if need.skill in finding.skills]
        supported = list(dict.fromkeys(sources.get(need.skill, [])))
        if supported:
            status = "in résumé sources"
        elif any(finding.can_support_claim for finding in found):
            status = "implementation found · contribution unconfirmed"
        elif found:
            status = "unsubstantiated by implementation"
        else:
            status = "no verified match"
        requirements.append(
            RequirementMatch(
                skill=need.skill,
                label=need.label,
                demand=need.requirement,
                quote=need.quote,
                option=need.option,
                resume_sources=supported[:8],
                finding_ids=[finding.id for finding in found],
                status=status,
            )
        )

    context: list[PostingContext] = []
    in_responsibilities = False
    for raw in job_description.splitlines():
        line = raw.strip().strip("#").strip()
        if _RESPONSIBILITIES.fullmatch(line):
            in_responsibilities = True
            continue
        if heading_kind(line) in {Demand.MANDATORY, Demand.PREFERRED} or (
            line.endswith(":") and len(line.split()) < 8
        ):
            in_responsibilities = False
        if line and in_responsibilities and len(context) < 12:
            context.append(
                PostingContext(
                    quote=line[:240],
                    kind="responsibility",
                    related_skills=[skill.label for skill in find_skills(line, CODE_SKILLS)],
                )
            )
    if posting.education:
        context.append(
            PostingContext(
                quote=posting.education.quote,
                kind="education",
                demand=posting.education.requirement,
            )
        )
    for demand in read_posting(job_description).demands[:8]:
        context.append(
            PostingContext(
                quote=demand.evidence.quote,
                kind="experience",
                demand=demand.demand,
            )
        )

    by_id = {project.id: project for project in projects}
    suggestions = []
    for finding in result.findings:
        project = by_id.get(finding.project_id)
        if not finding.can_support_claim or project is None:
            continue
        destination = claim_destination(project, chosen or set(), draft_resume or resume)
        if destination:
            name, entry = destination
            suggestions.append(
                ClaimSuggestion(
                    finding_id=finding.id,
                    project_name=name,
                    entry=entry,
                    wording=finding.wording,
                )
            )
    return CodeReview(
        requirements=requirements,
        context=context,
        findings=result.findings,
        limitations=result.limitations,
        suggestions=suggestions,
        repositories_checked=result.searched,
    )
