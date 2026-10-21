"""A list that asks for more than one thing: what matches all of it first, the rest after.

The owner asked the assistant "any software engineer roles in AI" on
2026-10-20. The role table read "software engineer"; "AI" is in 81% of
descriptions, so it was dropped as a search term, and the answer was 614
software engineer postings opening with three titled exactly "Software
Engineer" at Roku. Asked how it should work, the owner said: the exact roles
first, then the related ones after them, and nothing dropped, because a list
cut to what matched is where a missed job hides.

So a role question that asks for more is listed in tiers, each a heading of
its own:

- **In the title**: the role's postings whose title says the rest of the
  question ("Software Engineer, AI Platform").
- **Mentioned**: the ones whose description says it, when it is rare enough
  to search descriptions for ("Kafka"; never "AI").
- **The rest of the role**, closest to the question first.

A question answered from titles ("any roles for AI") lists the titles that say
the word, then the titles of a role whose name has the word and these do not:
"AI engineer" is a name of the AI / ML engineer role, so its "Machine Learning
Engineer" postings follow, under "related:".

The model is handed postings from the first tiers only when there are any, so
an answer cannot cite a posting for a word it does not say. The owner's list
under the answer carries every tier.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping

from packages.matching.roles import ROLE_ALIASES

IN_TITLE, MENTIONED, REST = 0, 1, 2

#: The heading a related role's postings are listed under, after the ones
#: whose title says the word.
RELATED_PREFIX = "related: "


def assign(
    title_stems: Mapping[uuid.UUID, frozenset[str]],
    wanted: frozenset[str],
    mentioned: Iterable[uuid.UUID] = (),
) -> dict[uuid.UUID, int]:
    """Each posting's tier: its title has every wanted word, its description
    was found saying one, or neither."""
    said = set(mentioned)
    return {
        posting_id: (
            IN_TITLE if wanted and wanted <= stems else MENTIONED if posting_id in said else REST
        )
        for posting_id, stems in title_stems.items()
    }


def matched(tier_of: Mapping[uuid.UUID, int]) -> int:
    """How many postings are in a tier above the rest."""
    return sum(1 for tier in tier_of.values() if tier != REST)


def relabel(
    label_of: Mapping[uuid.UUID, str],
    tier_of: Mapping[uuid.UUID, int],
    *,
    in_title: str,
    mentioned: str,
) -> dict[uuid.UUID, str]:
    """Each posting's kind, with its tier in the heading: "software engineer
    (AI in the title)", "AI engineer (mentions Kafka)". The rest keep theirs."""

    def heading(posting_id: uuid.UUID, label: str) -> str:
        tier = tier_of.get(posting_id, REST)
        if tier == IN_TITLE:
            return f"{label} ({in_title} in the title)"
        if tier == MENTIONED:
            return f"{label} (mentions {mentioned})"
        return label

    return {posting_id: heading(posting_id, label) for posting_id, label in label_of.items()}


def order_kinds(
    kinds: Iterable[tuple[str, int]],
    label_of: Mapping[uuid.UUID, str],
    tier_of: Mapping[uuid.UUID, int],
) -> list[tuple[str, int]]:
    """The kinds with each tier's ahead of the next. Stable, so the order
    within a tier is the one they came in."""
    tier_of_label: dict[str, int] = {}
    for posting_id, label in label_of.items():
        tier = tier_of.get(posting_id, REST)
        tier_of_label[label] = min(tier, tier_of_label.get(label, REST))
    return sorted(kinds, key=lambda kind: tier_of_label.get(kind[0], REST))


def roles_holding(stems: frozenset[str], stem_of: Callable[[str], str]) -> set[str]:
    """The roles with a name that has every one of `stems`. "AI" is in "AI
    engineer", so a question about AI is about that role's other titles too."""
    if not stems:
        return set()
    return {
        role
        for role, aliases in ROLE_ALIASES.items()
        for alias in aliases
        if stems <= {stem_of(word) for word in alias.split()}
    }


def split(
    ordered: list[uuid.UUID], tier_of: Mapping[uuid.UUID, int], limit: int, more: int
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """What the model is handed, and what is listed after it.

    Ordered by tier first. The model gets the first tiers' postings only, when
    there are any; the list carries the rest of them and every tier after, up
    to `more` of each. One cap for the whole list let 277 titles saying "AI"
    fill it, and the 43 related ones after them were never listed.
    """
    by_tier = sorted(ordered, key=lambda posting_id: tier_of.get(posting_id, REST))
    first = matched(tier_of)
    shown = min(limit, first) if first else limit
    listed: list[uuid.UUID] = []
    per_tier: dict[int, int] = {}
    for posting_id in by_tier[shown:]:
        tier = tier_of.get(posting_id, REST)
        if per_tier.get(tier, 0) < more:
            listed.append(posting_id)
            per_tier[tier] = per_tier.get(tier, 0) + 1
    return by_tier[:shown], listed


__all__ = [
    "IN_TITLE",
    "MENTIONED",
    "RELATED_PREFIX",
    "REST",
    "assign",
    "matched",
    "order_kinds",
    "relabel",
    "roles_holding",
    "split",
]
