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

from packages.matching.skill_vocab import BY_KEY, Skill, find_skills


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
    rf"(?:or\b|\(\s*or\b|(?:,\s*{_ITEM}){{1,4}},?\s+or\b|,\s*or\b)",
    re.IGNORECASE,
)
_OR_BEFORE = re.compile(rf"\bor\s+(?:an?\s+|the\s+)?(?:{_ITEM}\s*,\s*){{0,2}}$", re.IGNORECASE)
#: "Kafka/Kinesis". A name with a slash of its own ("CI/CD") is one skill, and
#: its pattern takes the slash with it, so none is left beside it here.
#: Offered as an instance of something wider: "a message queue such as Kafka".
#: "including" is left out: "tools including Kafka and Spark" asks for both.
_EXAMPLE_BEFORE = re.compile(
    rf"\b(?:such as|eg,?|for example,?|like|similar to)\s+(?:{_ITEM}\s*,\s*){{0,4}}"
    rf"(?:(?:and|or)\s+)?$",
    re.IGNORECASE,
)
_CLAUSE_END = re.compile(r"[.;!?]")
#: Where `requirements.extract` ends a clause: a full stop only before a space
#: or the end, so "Node.js" and ".NET" stay inside one. Used by the list rule
#: below; the older rules keep `_CLAUSE_END`, as they were measured with it.
_SENTENCE_END = re.compile(r"[;!?]|\.(?=\s|$)")
#: Its full stops are not the end of a clause.
_EG = re.compile(r"\be\.\s?g\.?", re.IGNORECASE)
_AND_OR = re.compile(r"\band\s*/\s*or\b", re.IGNORECASE)
#: A dotted release number immediately after a skill is part of its name,
#: not sentence punctuation or another list item: "PyTorch 2.0 or TensorFlow".
#: Leave any following full stop in place so "PyTorch 2.0. Or ..." stays two clauses.
_VERSION_AFTER = re.compile(r"^\s+v?\d+(?:\.(?:\d+|x))+\+?(?=\s|[,/;.!?)]|$)", re.IGNORECASE)
#: Quantified category choices can list alternatives without writing "or":
#: "at least one ML framework (PyTorch, TensorFlow)". Only the parenthetical
#: list is optional; another direct skill demand outside it is unchanged.
_QUANTIFIED_LIST = re.compile(
    r"\b(?:at least one|one or more|one of)\s+"
    r"(?:[\w-]{1,40}\s+){0,4}"
    r"(?:frameworks?|librar(?:y|ies)|languages?|tools?|databases?|platforms?)\s*\(",
    re.IGNORECASE,
)
#: The same choice without the quantifier: a plural category, then a bracketed
#: list of what counts as one, "public cloud platforms (AWS, GCP, Azure)" or
#: "modern web frameworks (React, Angular, Vue)". Measured on the live corpus
#: (2026-11-22): 293 of 374 direct GCP requirements were such lists. A list
#: joined by "and" names a stack, not a choice, and is left alone: "databases
#: (Postgres, Redis and ClickHouse)". "services" is not a category here:
#: "backend services (Java, Spring Boot, Kafka)" describes the team's stack.
_CATEGORY_LIST = re.compile(
    r"\b(?:frameworks|libraries|languages|tools|databases|platforms|providers"
    r"|environments|technologies|engines|clouds|vendors)\s*\(",
    re.IGNORECASE,
)
_CONJUNCTION = re.compile(r"\band\b|&", re.IGNORECASE)
#: "Linux, Bash, Go, or similar technologies": every item of a list that ends
#: in one of these is a choice. Read token by token (`_list_then_comparable`),
#: so the "or similar" is only reached across list items, never across a
#: clause: "SQL expertise and experience with Snowflake and dbt (or equivalent)"
#: leaves SQL required.
_COMPARABLE = frozenset({"comparable", "similar", "equivalent", "other", "alternative"})
_TOKEN = re.compile(r"\s*([(),]|[\w.+#/-]+|\S)")
_WORD = re.compile(r"[\w.+#/-]+")
#: Words one list item may run to: "lakehouse technologies", "Apache Spark".
_ITEM_WORDS = 4
#: A list item is a name. These make it a phrase: "Excel and PowerPoint, with
#: experience in Python, SQL, R or similar" offers the tools, not Excel.
_NOT_IN_A_NAME = frozenset(
    {
        *("with", "in", "using", "for", "of", "to", "on", "from", "by", "as", "at", "via"),
        *("including", "like", "such", "especially"),
    }
)


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


def _or_after(text: str) -> bool:
    # Trim once outside the regex: overlapping whitespace quantifiers before
    # "or" made a long, nonmatching whitespace suffix quadratic.
    text = text.lstrip()
    if _THEN_OR.match(text):
        return True
    if text.startswith("("):
        end = text.find(")")
        if end != -1:
            return _THEN_OR.match(text[end + 1 :].lstrip()) is not None
    return False


def _list_then_comparable(after: str) -> bool:
    """Whether the skill is offered with "or similar": its own, or its list's.

    `after` is the rest of the skill's clause. A bracketed "(or similar …)"
    belongs to the item right before it, so it counts only straight after the
    skill: "Snowflake, Kafka (or similar streaming platforms)" offers Kafka,
    not Snowflake. A trailing ", or similar" belongs to a comma list, reached
    across list items only: never across a phrase like "with experience in".
    """
    tokens = [found.group(1).lower() for found in _TOKEN.finditer(after[:400])]
    if tokens[:2] == ["(", "or"]:
        return _comparable(tokens[2:])
    if not tokens or tokens[0] not in {",", "and"}:
        return False
    commas = words = 0
    for index, token in enumerate(tokens):
        if token == "or":
            return commas > 0 and _comparable(tokens[index + 1 :])
        if token in {",", "and"}:
            commas += token == ","
            words = 0
            continue
        if token in _NOT_IN_A_NAME or not _WORD.fullmatch(token):
            return False
        words += 1
        if words > _ITEM_WORDS:
            return False
    return False


def _comparable(tokens: list[str]) -> bool:
    if tokens[:1] == ["any"]:
        tokens = tokens[1:]
    return bool(tokens) and tokens[0] in _COMPARABLE


def _slash_after(text: str) -> bool:
    after = text.lstrip()
    if not after.startswith("/"):
        return False
    name = after[1:].lstrip()
    return bool(name and name[0].isascii() and name[0].isalpha())


def _slash_before(text: str) -> bool:
    before = text.rstrip()
    if not before.endswith("/"):
        return False
    name = before[:-1].rstrip()
    return bool(name and name[-1].isascii() and name[-1].isalpha())


def is_option(quote: str, skill: Skill) -> bool:
    """Whether `quote` offers `skill` as a choice among others, or as an example.

    Read inside the clause the skill is in: "Python or Go required; Kafka a
    plus" offers Python and does not offer Kafka.
    """
    quote = _AND_OR.sub("or", _EG.sub("eg", quote))
    quantified_choices = []
    for heading in _QUANTIFIED_LIST.finditer(quote):
        end = quote.find(")", heading.end())
        if end != -1 and len({entry.key for entry in find_skills(quote[heading.end() : end])}) > 1:
            quantified_choices.append((heading.end(), end))
    for heading in _CATEGORY_LIST.finditer(quote):
        end = quote.find(")", heading.end())
        listed = quote[heading.end() : end] if end != -1 else ""
        if (
            listed
            and not _CONJUNCTION.search(listed)
            and len({entry.key for entry in find_skills(listed)}) > 1
        ):
            quantified_choices.append((heading.end(), end))
    quantified_option = False
    direct_outside_choice = False
    for match in skill.pattern.finditer(quote):
        in_choice = any(
            start <= match.start() and match.end() <= end for start, end in quantified_choices
        )
        quantified_option |= in_choice
        before = quote[: match.start()]
        after = _VERSION_AFTER.sub("", quote[match.end() :], count=1)
        ends = [found.end() for found in _CLAUSE_END.finditer(before)]
        clause_before = before[ends[-1] :] if ends else before
        stop = _CLAUSE_END.search(after)
        clause_after = after[: stop.start()] if stop else after
        sentence_stop = _SENTENCE_END.search(after)
        sentence_after = after[: sentence_stop.start()] if sentence_stop else after
        if (
            _or_after(clause_after)
            or _slash_after(clause_after)
            or _OR_BEFORE.search(clause_before)
            or _slash_before(clause_before)
            or _EXAMPLE_BEFORE.search(clause_before)
            or _list_then_comparable(sentence_after)
        ):
            return True
        if not in_choice:
            direct_outside_choice = True
    # The quantified list cannot weaken a separate direct occurrence of this
    # skill in the same quote. Existing or/slash/example readings stay intact.
    return quantified_option and not direct_outside_choice


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
