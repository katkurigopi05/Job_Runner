"""What the assistant's context and reply say about their own evidence.

Two small things `routers/chat.py` uses, kept out of it because that file is
already past the size it should be:

- for a skill the question named, how each retrieved posting asks for it
  (`packages/matching/evidence.py`), as a line under the posting;
- which postings an answer cites that were never handed to the model.

Neither judges the answer. They state what can be known without reading the
prose for its meaning.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from packages.matching.evidence import skill_evidence
from packages.matching.retrieve import Retrieval
from packages.matching.skill_vocab import Skill, find_skills

#: Skills explained per question. One is the usual number; a question naming
#: six is a pasted list, and six lines under each of five postings is most of
#: a local model's context.
SKILLS_EXPLAINED = 2


def skills_asked_about(question: str) -> list[Skill]:
    """The vocabulary skills a question names, the first few."""
    return find_skills(question)[:SKILLS_EXPLAINED]


def skill_lines(
    found: Retrieval,
    skills: Iterable[Skill],
    requirements: Mapping[uuid.UUID, Mapping[str, Any] | None],
) -> dict[uuid.UUID, list[str]]:
    """Per retrieved posting: how it asks for each skill the question named.

    A posting whose requirements have not been read says so. Silence there
    would be read as "does not ask for it".
    """
    asked = list(skills)
    lines: dict[uuid.UUID, list[str]] = {}
    for passage in found.passages:
        for skill in asked:
            evidence = skill_evidence(requirements.get(passage.posting_id), skill.key)
            lines.setdefault(passage.posting_id, []).append(
                evidence.in_words()
                if evidence is not None
                else f"{skill.label} here: this posting's requirements have not been read yet"
            )
    return lines


def unsupported_citations(cited: Iterable[str], supplied: Iterable[str]) -> list[str]:
    """Labels an answer cites that were not among the postings it was handed.

    "P7" over five postings is a source the model made up. In label order, so
    the reply is the same each time. A label that *was* supplied is not thereby
    a claim that is supported: this finds the one kind of unsupported citation
    that needs no reading to find.
    """
    known = set(supplied)
    invented = {label for label in cited if label not in known}
    return sorted(invented, key=lambda label: int(label[1:]))


__all__ = ["SKILLS_EXPLAINED", "skill_lines", "skills_asked_about", "unsupported_citations"]
