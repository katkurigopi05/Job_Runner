"""Job titles that name the same role differently.

`Software Engineer`, `SDE II`, and `Member of Technical Staff` are one role and
three strings. Nothing upstream of this module knew that: the keyword filter in
`search.py` is a substring test, and the title half of the score in `score.py`
is a cosine over `embed.py`, whose shipped backend is a hashed bag of words. On
that backend `SDE II` scores 0.000 against `Software Engineer` — the same as
`Dental Hygienist`, because the two share no token and hashing is not meaning.

Switching `EMBEDDING_BACKEND` to sentence-transformers narrows that gap but does
not close it. On the same eleven titles it puts `SDE II` at 0.645 and an
unrelated healthcare title at 0.582: the ordering is right, the margin is six
hundredths, and a threshold placed in that band is noise. Short strings carry
too little context for a sentence embedder to separate confidently.

So the aliases here are **curated, not learned**, and that is the point rather
than a shortcut. The distinctions the owner cares about are exactly the ones
distributional similarity blurs: `Data Engineer` and `Data Scientist` read as
near-identical to any embedding trained on English, and treating them as one
role is a worse failure than missing a synonym. A table can hold that line. A
cosine cannot.

`mine_aliases` exists for the other half of the problem — a table nobody
extends goes stale — and it *proposes* to a human. It never writes here.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from packages.core.models import Posting

#: Canonical role -> the surface forms that mean it.
#:
#: Membership is a claim that two titles are the *same job*, not that they are
#: adjacent. When unsure, leave a title out: an absent alias costs a posting
#: that still reaches the feed by body similarity, while a wrong one silently
#: merges two roles and the owner never sees that it happened.
ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    "software_engineer": (
        "software engineer",
        "software developer",
        "software development engineer",
        "sde",
        "swe",
        "programmer",
        "programmer analyst",
        "computer programmer",
        "application developer",
        "applications developer",
        "member of technical staff",
        "mts",
    ),
    "backend_engineer": (
        "backend engineer",
        "backend developer",
        "back end engineer",
        "back end developer",
        "server side engineer",
        "api engineer",
        "api developer",
    ),
    "frontend_engineer": (
        "frontend engineer",
        "frontend developer",
        "front end engineer",
        "front end developer",
        "ui engineer",
        "ui developer",
        "web developer",
    ),
    "fullstack_engineer": (
        "full stack engineer",
        "full stack developer",
        "fullstack engineer",
        "fullstack developer",
    ),
    "data_engineer": (
        "data engineer",
        "etl developer",
        "etl engineer",
        "data warehouse engineer",
        "data warehouse developer",
        "analytics engineer",
        "big data engineer",
        "data platform engineer",
        "data infrastructure engineer",
        "data pipeline engineer",
    ),
    "data_scientist": (
        "data scientist",
        "applied scientist",
        "research scientist",
        "decision scientist",
    ),
    "machine_learning_engineer": (
        "machine learning engineer",
        "ml engineer",
        "mle",
        "ai engineer",
        "artificial intelligence engineer",
        "deep learning engineer",
        "applied machine learning engineer",
        "applied ml engineer",
    ),
    "data_analyst": (
        "data analyst",
        "business intelligence analyst",
        "bi analyst",
        "business intelligence developer",
        "bi developer",
        "reporting analyst",
        "analytics analyst",
    ),
    "devops_engineer": (
        "devops engineer",
        "devops",
        "site reliability engineer",
        "sre",
        "platform engineer",
        "infrastructure engineer",
        "cloud engineer",
    ),
}

#: Surface form -> canonical role. Built once; the table above is the source.
_ALIAS_TO_ROLE: dict[str, str] = {
    alias: role for role, aliases in ROLE_ALIASES.items() for alias in aliases
}

#: Longest first, so "machine learning engineer" is read before "engineer"
#: would ever be, and "data engineer" is never swallowed by "data analyst".
_ALIASES_BY_LENGTH: tuple[str, ...] = tuple(
    sorted(_ALIAS_TO_ROLE, key=lambda alias: (-len(alias.split()), -len(alias)))
)

#: Rung words and level markers. `search.py` reads the rung off a title to
#: decide whether the owner asked for it; here the same vocabulary is removed,
#: because "Senior Data Engineer" and "Data Engineer" are one role at two
#: levels and seniority is already a filter of its own.
_LEVEL_RE = re.compile(
    r"\b("
    r"intern|internship|junior|jr|entry[ -]level|associate|new[ -]grad|"
    r"senior|sr|staff|lead|principal|distinguished|fellow|"
    r"i{1,3}|iv|v|vi{1,3}|ix|x|"
    r"l\d|ic\d|t\d|e\d|g\d|\d+"
    r")\b",
    re.I,
)

#: Trailing or bracketed qualifiers: "(Remote)", "- New York", "| Platform".
_QUALIFIER_RE = re.compile(r"[(\[{].*?[)\]}]|[-–—|,/].*$")

_PUNCT_RE = re.compile(r"[^a-z0-9+#. ]+")
_SPACE_RE = re.compile(r"\s+")


def _surface(title: str) -> str:
    """A title with punctuation and trailing qualifiers gone, levels intact.

    Levels stay because a rung word can be *part of a role name*: stripping
    "staff" first turns "Member of Technical Staff" into "member of technical"
    and loses the alias entirely. `canonical` reads this form before it reads
    the unlevelled one, so a title that is only recognizable with its rung
    words still resolves.
    """
    text = _QUALIFIER_RE.sub(" ", title.lower())
    return _SPACE_RE.sub(" ", _PUNCT_RE.sub(" ", text)).strip()


def normalize(title: str) -> str:
    """A title reduced to its role words: lowercased, unlevelled, unqualified.

    >>> normalize("Senior Software Engineer II (Remote) - Platform")
    'software engineer'
    """
    return _SPACE_RE.sub(" ", _LEVEL_RE.sub(" ", _surface(title))).strip()


def _lookup(text: str) -> str | None:
    """Exact alias, else the longest alias appearing as whole words."""
    if not text:
        return None
    if (exact := _ALIAS_TO_ROLE.get(text)) is not None:
        return exact
    padded = f" {text} "
    for alias in _ALIASES_BY_LENGTH:
        if f" {alias} " in padded:
            return _ALIAS_TO_ROLE[alias]
    return None


def canonical(title: str) -> str | None:
    """The canonical role a title names, or None when no alias is recognized.

    None is a real answer and is never treated as a match: an unreadable title
    must not quietly become whatever role happened to be asked for.
    """
    return _lookup(_surface(title)) or _lookup(normalize(title))


def roles_in(text: str) -> set[str]:
    """Every canonical role named anywhere in a block of text.

    Used on a résumé, this is the set of roles the owner has actually held —
    which is what makes a posting's title a match rather than a coincidence.
    """
    haystack = f" {_SPACE_RE.sub(' ', _PUNCT_RE.sub(' ', text.lower()))} "
    return {_ALIAS_TO_ROLE[alias] for alias in _ALIASES_BY_LENGTH if f" {alias} " in haystack}


def named_in(text: str) -> dict[str, set[str]]:
    """The canonical roles a text names, each with the aliases that named it.

    `roles_in` with its evidence. The assistant's search needs it twice over:
    in "AI engineer jobs using Kafka" the alias says which role and which kind
    of that role ("AI engineer", not "machine learning engineer"), and what is
    left of the question is what to search that role's postings for.
    """
    haystack = f" {_SPACE_RE.sub(' ', _PUNCT_RE.sub(' ', text.lower()))} "
    named: dict[str, set[str]] = {}
    for alias in _ALIASES_BY_LENGTH:
        if f" {alias} " in haystack:
            named.setdefault(_ALIAS_TO_ROLE[alias], set()).add(alias)
    return named


#: What a role is called on screen, where its key does not say it. One role in
#: the table is two words to the owner: asked for "AI engineer" on 2026-10-06
#: the assistant answered about "machine learning engineer" postings.
_DISPLAY_NAMES = {"machine_learning_engineer": "AI / ML engineer"}

#: Written as initials, however an alias spells them.
_INITIALS = {
    "ai": "AI",
    "ml": "ML",
    "mle": "MLE",
    "mts": "MTS",
    "sde": "SDE",
    "swe": "SWE",
    "sre": "SRE",
    "bi": "BI",
    "etl": "ETL",
    "api": "API",
    "ui": "UI",
    "devops": "DevOps",
}


def display_name(role: str) -> str:
    """A canonical role in words: "AI / ML engineer", "data engineer"."""
    return _DISPLAY_NAMES.get(role, role.replace("_", " "))


def alias_label(alias: str) -> str:
    """An alias as a heading: "ai engineer" is "AI engineer"."""
    return " ".join(_INITIALS.get(word, word) for word in alias.split())


def kind_of(title: str) -> str | None:
    """The kind of role a title names, by the alias it was recognised by.

    A role in the table is several kinds of title. On the owner's corpus the
    61 AI / ML engineer postings were 41 "machine learning engineer", 18 "AI
    engineer", one "deep learning engineer" and one "applied ML engineer", and
    the first two ask for different things: LLMs and TypeScript against PyTorch
    and TensorFlow. Calling them one role is right for the feed's filter and
    wrong for a list the owner reads. None when no alias is recognised.
    """
    for form in (_surface(title), normalize(title)):
        if not form:
            continue
        if form in _ALIAS_TO_ROLE:
            return alias_label(form)
        padded = f" {form} "
        for alias in _ALIASES_BY_LENGTH:
            if f" {alias} " in padded:
                return alias_label(alias)
    return None


#: Roles worth a look after this one. Neighbours, never synonyms: nothing here
#: merges two roles, and the alias table above is untouched. Machine learning
#: engineer and data scientist are 0.80 alike by the skills their postings ask
#: for, which is as alike as the two kinds of AI / ML engineer are.
_RELATED = {
    "software_engineer": ("backend_engineer", "fullstack_engineer", "frontend_engineer"),
    "backend_engineer": ("software_engineer", "fullstack_engineer", "devops_engineer"),
    "frontend_engineer": ("fullstack_engineer", "software_engineer"),
    "fullstack_engineer": ("software_engineer", "backend_engineer", "frontend_engineer"),
    "data_engineer": ("machine_learning_engineer", "data_analyst", "data_scientist"),
    "data_scientist": ("machine_learning_engineer", "data_analyst", "data_engineer"),
    "machine_learning_engineer": ("data_scientist", "data_engineer"),
    "data_analyst": ("data_scientist", "data_engineer"),
    "devops_engineer": ("backend_engineer", "software_engineer"),
}


def related_to(role: str) -> tuple[str, ...]:
    """Neighbouring roles, nearest first. Empty for a role with none listed."""
    return _RELATED.get(role, ())


def same_role(left: str, right: str) -> bool:
    """Whether two titles name one role. Unrecognized titles never match."""
    canonical_left = canonical(left)
    return canonical_left is not None and canonical_left == canonical(right)


@dataclass(frozen=True)
class AliasProposal:
    """A title pair the corpus suggests are the same role. Never auto-adopted."""

    canonical_title: str
    proposed_alias: str
    #: How alike the two postings' bodies were, averaged over the pairs seen.
    body_similarity: float
    #: How many posting pairs supported it. One pair is a coincidence.
    support: int

    def as_line(self) -> str:
        return (
            f"{self.proposed_alias!r} ~ {self.canonical_title!r} "
            f"(similarity {self.body_similarity:.2f}, {self.support} pairs)"
        )


def mine_aliases(
    postings: list[Posting],
    *,
    threshold: float = 0.82,
    min_support: int = 2,
) -> list[AliasProposal]:
    """Propose aliases from postings whose bodies match but whose titles do not.

    The distributional argument, applied to the corpus actually crawled rather
    than to general English: two companies describing the same work in nearly
    the same words are naming one role, whatever they chose to call it. That is
    a far narrower claim than "these strings embed close together", because the
    evidence is a whole job description rather than three words.

    It returns proposals. Adopting one means editing `ROLE_ALIASES` by hand,
    on purpose — see this module's docstring for why the table is curated.
    """
    from packages.matching.embed import cosine, get_embedder

    usable = [p for p in postings if (p.title or "").strip() and (p.description_raw or "").strip()]
    if len(usable) < 2:
        return []

    embedder = get_embedder()
    vectors = embedder.encode([p.description_raw or "" for p in usable])

    pairs: dict[tuple[str, str], list[float]] = defaultdict(list)
    for i, left in enumerate(usable):
        for j in range(i + 1, len(usable)):
            right = usable[j]
            left_role, right_role = canonical(left.title or ""), canonical(right.title or "")
            # Already agreed, or the pair says nothing about an unknown title.
            if left_role is not None and left_role == right_role:
                continue
            if (left_role is None) == (right_role is None):
                continue
            similarity = cosine(vectors[i], vectors[j])
            if similarity < threshold:
                continue
            known, unknown = (left, right) if left_role is not None else (right, left)
            pairs[(known.title or "", unknown.title or "")].append(similarity)

    proposals = [
        AliasProposal(
            canonical_title=known_title,
            proposed_alias=normalize(unknown_title) or unknown_title,
            body_similarity=sum(scores) / len(scores),
            support=len(scores),
        )
        for (known_title, unknown_title), scores in pairs.items()
        if len(scores) >= min_support
    ]
    return sorted(proposals, key=lambda p: (-p.support, -p.body_similarity))
