"""How a posting asks for a skill: required, preferred, an option, or only named.

The assistant is handed an excerpt that contains the word, and "Which jobs use
Kafka?" was being answered as though containing it were asking for it. A
posting that says "we migrated off Kafka", one that lists "Kafka or Kinesis",
and one with Kafka under "Requirements" are three different answers, and the
excerpt alone shows a model the word in all three.

Nothing here reads a posting again. `requirements.py` already read each one at
ingestion, by the heading a skill sat under, and stored one mention a skill at
its strongest with the line it came from. This turns that record into a
sentence the model can repeat, and adds the one thing the record does not
hold: whether the line offers the skill as one choice among several.

**An option is read from the quoted line and nothing else.** "Experience with
Kafka or Kinesis" under "Requirements" is required as a pair: either will do.
Counting it as "requires Kafka" is how an applicant with Kinesis is told they
have a gap. The line is always quoted beside the reading, so a reader can
check it, and a line this misreads is at worst a required skill reported as
required.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from packages.matching.skill_vocab import BY_KEY, Skill


class Strength(StrEnum):
    REQUIRED = "required"
    PREFERRED = "preferred"
    #: Named under no heading that says whether it is asked for: "we build
    #: with Go" in a paragraph about the company.
    NAMED = "named"
    #: Not among the skills read from the posting. The search may still have
    #: found the word in it, in a spelling the vocabulary does not hold.
    UNREAD = "unread"


#: As `requirements.py` stores them, strongest first.
_STORED = (
    ("required", Strength.REQUIRED),
    ("preferred", Strength.PREFERRED),
    ("unclassified", Strength.NAMED),
)

#: Items between the skill and an "or", each short enough to be another name
#: in the list and not the rest of the sentence: "Kafka, Kinesis, or Pub/Sub".
_ITEM = r"[\w.+#/-]+(?: [\w.+#/-]+){0,2}"
_THEN_OR = re.compile(
    rf"\s*(?:\(\s*or\b|(?:\([^)]*\)\s*)?(?:,\s*{_ITEM}){{0,4}},?\s+or\b)", re.IGNORECASE
)
_OR_BEFORE = re.compile(rf"\bor\s+(?:an?\s+|the\s+)?(?:{_ITEM}\s*,\s*){{0,2}}$", re.IGNORECASE)
#: "Kafka/Kinesis". A name with a slash of its own ("CI/CD") is one skill, and
#: its pattern takes the slash with it, so none is left beside it here.
_SLASH_AFTER = re.compile(r"\s*/\s*[A-Za-z]")
_SLASH_BEFORE = re.compile(r"[A-Za-z]\s*/\s*$")
#: Offered as an instance of something wider: "a message queue such as Kafka".
#: "including" is left out: "tools including Kafka and Spark" asks for both.
_EXAMPLE_BEFORE = re.compile(
    rf"\b(?:such as|eg,?|for example,?|like|similar to)\s+(?:{_ITEM}\s*,\s*){{0,4}}"
    rf"(?:(?:and|or)\s+)?$",
    re.IGNORECASE,
)
_CLAUSE_END = re.compile(r"[.;!?]")
#: Its full stops are not the end of a clause.
_EG = re.compile(r"\be\.\s?g\.?", re.IGNORECASE)
#: A dotted release number immediately after a skill is part of its name,
#: not sentence punctuation or another list item: "PyTorch 2.0 or TensorFlow".
#: Leave any following full stop in place so "PyTorch 2.0. Or ..." stays two clauses.
_VERSION_AFTER = re.compile(r"^\s+v?\d+(?:\.(?:\d+|x))+\+?(?=\s|[,/;.!?)]|$)", re.IGNORECASE)


@dataclass(frozen=True)
class SkillEvidence:
    skill: str
    label: str
    strength: Strength
    #: The line offers it as one choice among several, or as an example.
    option: bool = False
    #: The posting's line, as `requirements.py` quoted it. None when unread.
    quote: str | None = None

    def in_words(self) -> str:
        """One line for the model to repeat: the reading, then the line it was read from."""
        if self.strength is Strength.UNREAD:
            return f"{self.label} here: not among the skills read from this posting's requirements"
        reading = {
            Strength.REQUIRED: "required",
            Strength.PREFERRED: "preferred, not required",
            Strength.NAMED: "named, under no requirements heading, so not shown to be asked for",
        }[self.strength]
        if self.option:
            reading += "; offered as one of several options, so not this one specifically"
        return f'{self.label} here: {reading} — "{self.quote}"'


def is_option(quote: str, skill: Skill) -> bool:
    """Whether `quote` offers `skill` as a choice among others, or as an example.

    Read inside the clause the skill is in: "Python or Go required; Kafka a
    plus" offers Python and does not offer Kafka.
    """
    quote = _EG.sub("eg", quote)
    for match in skill.pattern.finditer(quote):
        before = quote[: match.start()]
        after = _VERSION_AFTER.sub("", quote[match.end() :], count=1)
        ends = [found.end() for found in _CLAUSE_END.finditer(before)]
        clause_before = before[ends[-1] :] if ends else before
        stop = _CLAUSE_END.search(after)
        clause_after = after[: stop.start()] if stop else after
        if (
            _THEN_OR.match(clause_after)
            or _SLASH_AFTER.match(clause_after)
            or _OR_BEFORE.search(clause_before)
            or _SLASH_BEFORE.search(clause_before)
            or _EXAMPLE_BEFORE.search(clause_before)
        ):
            return True
    return False


def skill_evidence(requirements: Mapping[str, Any] | None, skill_key: str) -> SkillEvidence | None:
    """How one posting asks for one skill, or None when the posting is unread.

    None is "requirements not read yet", which is not "does not ask for it".
    """
    if requirements is None:
        return None
    skill = BY_KEY.get(skill_key)
    label = skill.label if skill is not None else skill_key
    stored = requirements.get("skills") or {}
    for name, strength in _STORED:
        for entry in stored.get(name) or []:
            if entry.get("skill") != skill_key:
                continue
            quote = str(entry.get("quote") or "")
            return SkillEvidence(
                skill=skill_key,
                label=label,
                strength=strength,
                option=(
                    entry["option"]
                    if type(entry.get("option")) is bool
                    else bool(skill is not None and quote and is_option(quote, skill))
                ),
                quote=quote,
            )
    return SkillEvidence(skill=skill_key, label=label, strength=Strength.UNREAD)


__all__ = ["SkillEvidence", "Strength", "is_option", "skill_evidence"]
