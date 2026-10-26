"""Demand accounting for an explicit, single-skill requirement question.

The shared skill vocabulary supplies the complete match set. Each match has its own
requirement evidence; a preferred skill or an alternative is not a direct demand.
Stale extraction is refreshed in memory, never written to the live database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from packages.matching.embed import tokenize
from packages.matching.evidence import Strength, skill_evidence
from packages.matching.requirements import EXTRACTOR_VERSION, extract
from packages.matching.skill_vocab import Skill, find_skills

_REQUIRED = re.compile(r"\b(?:require[ds]?|requiring|must[- ]have)\b", re.I)


def requested_skill(question: str, terms: list[str]) -> Skill | None:
    """Only one explicit skill demand; ordinary mention and compound queries keep ranking."""
    skills = find_skills(question)
    if len(skills) != 1 or not _REQUIRED.search(question):
        return None
    if re.search(r"\b(?:not|never)\s+(?:require[ds]?|required)\b", question, re.I):
        return None
    skill = skills[0]
    surfaces = {
        word for match in skill.pattern.finditer(question) for word in tokenize(match.group())
    }
    return skill if set(terms) <= surfaces else None


def fresh_requirements(
    stored: dict[str, Any] | None, version: int | None, description: str | None
) -> dict[str, Any] | None:
    if stored is not None and version == EXTRACTOR_VERSION:
        return stored
    return extract(description).as_json() if description and description.strip() else None


def demand_class(requirements: dict[str, Any] | None, skill: Skill) -> tuple[str, str | None]:
    evidence = skill_evidence(requirements, skill.key)
    if evidence is None:
        return "unread", None
    if evidence.strength is Strength.REQUIRED:
        return ("options" if evidence.option else "required"), evidence.quote
    if evidence.strength is Strength.PREFERRED:
        return "preferred", evidence.quote
    return "named", evidence.quote


@dataclass(frozen=True)
class RequiredSkillCount:
    skill: str
    required: int
    options: int
    preferred: int
    named: int
    unread: int
    companies: tuple[str, ...]
    company_total: int
    extractor_version: int = EXTRACTOR_VERSION

    @property
    def complete(self) -> bool:
        return self.unread == 0

    def in_words(self) -> str:
        reach = "" if self.complete else "At least "
        firms = ", ".join(self.companies) or "none recorded"
        return (
            f"{reach}{self.required} postings directly require {self.skill}, "
            f"at {self.company_total} companies in the applied search scope.\n"
            f"Companies with direct requirements: {firms}.\n"
            f"Other mentions: {self.options} offer alternatives, {self.preferred} prefer it, "
            f"{self.named} do not establish a direct requirement; {self.unread} unread. "
            f"Requirement evidence uses extractor v{self.extractor_version}. "
            + (
                "Coverage is complete."
                if self.complete
                else "The exhaustive required total is unknown."
            )
        )
