"""The labeled sets the scorer is measured against, and where they came from.

CLAUDE.md §15 already says the uncomfortable thing about Gate 5: its twenty
postings "are not postings the owner labeled". They were written to read like
real ones, next to the code that reads them. This module does not fix that —
only the owner labeling real postings fixes that — but it makes the problem
visible at the point of use instead of in a docstring, by refusing to load a
set that does not declare where its labels came from.

## Provenance is required, not optional

Every item carries a `Provenance`. `FIXTURE` means someone wrote the posting
and its label together; `OWNER` means the owner labeled a posting that a crawl
actually returned; `FEEDBACK` means the label was derived from a decision the
owner made in the feed. A benchmark run reports the mix, and a claim resting
on `FIXTURE` labels is worth what a fixture is worth.

The loader raises on a missing `provenance` field rather than defaulting it.
A default would be `FIXTURE` — the safe value — and a set of real owner labels
that lost its provenance in an edit would then quietly understate itself,
which is the one direction of error that makes the eventual real numbers look
like fake ones.

## Splitting without leaking

`split()` groups by company before it splits. Two postings from the same
employer share boilerplate — benefits paragraphs, the same "about us", often
whole requirement lists — so putting one in train and its sibling in test
lets a model recognise the company rather than the job. The master spec calls
this company leakage; it is the easiest kind to introduce here and the hardest
to see afterwards, because the metrics simply come out too good.

Splitting is deterministic on a seed and on the group name, so a set that
grows by one posting does not reshuffle everything that came before it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "LabeledPosting",
    "LabeledSet",
    "Provenance",
    "dump_labeled_set",
    "load_labeled_set",
]


class Provenance(StrEnum):
    """Where a relevance label came from. See the module docstring."""

    #: Posting and label written together, for a test. Proves a regression,
    #: not a capability.
    FIXTURE = "fixture"
    #: A real posting the owner read and graded.
    OWNER = "owner"
    #: Derived from an owner decision in the feed — applied, skipped, hidden.
    FEEDBACK = "feedback"


#: Graded relevance. The gap from "would apply" to "would drop everything for"
#: is deliberately wider than the gap from "no" to "maybe", because `2**rel`
#: gain in `metrics.dcg_at_k` is what turns that into ranking pressure on the
#: first three slots.
RELEVANCE_SCALE: dict[int, str] = {
    0: "irrelevant — would not apply",
    1: "plausible — would read the posting",
    2: "good — would apply",
    3: "excellent — would apply today",
}


@dataclass(frozen=True)
class LabeledPosting:
    """One posting with a human relevance grade attached."""

    key: str
    title: str
    description: str
    relevance: int
    provenance: Provenance
    company: str = ""
    location: str = "Remote"
    #: Free-form. Why this grade — the thing that lets a disagreement be
    #: settled later instead of re-litigated from scratch.
    note: str = ""
    #: Tags select subsets. `gate5` is the set CLAUDE.md §9's Gate 5 asserts
    #: against; adding items to the file must not silently change that gate,
    #: so the gate selects by tag rather than taking everything.
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.relevance not in RELEVANCE_SCALE:
            raise ValueError(
                f"{self.key}: relevance {self.relevance} is outside the scale "
                f"{sorted(RELEVANCE_SCALE)}"
            )


@dataclass(frozen=True)
class LabeledSet:
    """A versioned corpus of labeled postings for one profile."""

    name: str
    version: str
    profile_text: str
    items: tuple[LabeledPosting, ...]
    description: str = ""

    @property
    def digest(self) -> str:
        """Content hash of the labels, for the experiment record.

        A metric is meaningless without the dataset it was computed on, and
        "labeled_matches.yaml" is not an identifier — the file changes. This
        is what `benchmark.ExperimentRecord.dataset_digest` stores so a number
        from last month can be checked against the data that produced it.
        """
        payload = json.dumps(
            [
                [i.key, i.title, i.description, i.relevance, i.provenance.value]
                for i in sorted(self.items, key=lambda i: i.key)
            ],
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @property
    def provenance_mix(self) -> dict[str, int]:
        """How many labels of each kind. Reported beside every result."""
        mix: dict[str, int] = {}
        for item in self.items:
            mix[item.provenance.value] = mix.get(item.provenance.value, 0) + 1
        return mix

    @property
    def is_fixture_only(self) -> bool:
        """True when nothing here was labeled against a real posting.

        `benchmark.summarize` reads this and marks the result accordingly.
        The number is still a valid regression signal; it is not evidence
        about the owner's actual feed.
        """
        return all(i.provenance is Provenance.FIXTURE for i in self.items)

    def tagged(self, tag: str) -> tuple[LabeledPosting, ...]:
        return tuple(i for i in self.items if tag in i.tags)

    def split(
        self, *, holdout: float = 0.3, seed: int = 0
    ) -> tuple[tuple[LabeledPosting, ...], tuple[LabeledPosting, ...]]:
        """Group-aware train/holdout split. Companies never straddle the line.

        A posting with no company is its own group — an unnamed employer
        cannot be shown to be the same one twice, and merging them all into a
        single "" group would put the whole set on one side of the split.
        """
        if not 0.0 < holdout < 1.0:
            raise ValueError("holdout must be between 0 and 1")
        groups: dict[str, list[LabeledPosting]] = {}
        for item in self.items:
            key = item.company.strip().lower() or f"__ungrouped__{item.key}"
            groups.setdefault(key, []).append(item)

        # Deterministic in the group name rather than in iteration order, so
        # adding a posting does not move unrelated companies across the line.
        def rank(name: str) -> str:
            return hashlib.sha256(f"{seed}:{name}".encode()).hexdigest()

        ordered = sorted(groups, key=rank)
        target = round(len(self.items) * holdout)
        held: list[LabeledPosting] = []
        for name in ordered:
            if len(held) >= target:
                break
            held.extend(groups[name])
        held_keys = {i.key for i in held}
        train = tuple(i for i in self.items if i.key not in held_keys)
        return train, tuple(held)


def _require(raw: dict[str, Any], key: str, where: str) -> Any:
    if key not in raw:
        raise ValueError(f"{where}: missing required field {key!r}")
    return raw[key]


def dump_labeled_set(labeled: LabeledSet, path: str | Path) -> Path:
    """Write a labeled set as YAML that `load_labeled_set` reads back.

    A writer next to the reader, so the two cannot drift into disagreeing
    about the schema — the round trip is asserted in the tests.
    """
    location = Path(path)
    payload = {
        "name": labeled.name,
        "version": labeled.version,
        "profile_text": labeled.profile_text,
        "description": labeled.description,
        "postings": [
            {
                "key": item.key,
                "title": item.title,
                "description": item.description,
                "relevance": item.relevance,
                "provenance": item.provenance.value,
                "company": item.company,
                "location": item.location,
                "note": item.note,
                "tags": list(item.tags),
            }
            for item in labeled.items
        ],
    }
    location.parent.mkdir(parents=True, exist_ok=True)
    location.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    return location


def load_labeled_set(path: str | Path) -> LabeledSet:
    """Read a labeled set from YAML, refusing anything under-declared."""
    location = Path(path)
    raw = yaml.safe_load(location.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{location}: expected a mapping at the top level")

    items: list[LabeledPosting] = []
    seen: set[str] = set()
    for index, entry in enumerate(_require(raw, "postings", str(location))):
        where = f"{location}[{index}]"
        key = str(_require(entry, "key", where))
        if key in seen:
            raise ValueError(f"{where}: duplicate key {key!r}")
        seen.add(key)
        provenance_raw = _require(entry, "provenance", where)
        try:
            provenance = Provenance(provenance_raw)
        except ValueError:
            raise ValueError(
                f"{where}: unknown provenance {provenance_raw!r}; "
                f"expected one of {[p.value for p in Provenance]}"
            ) from None
        items.append(
            LabeledPosting(
                key=key,
                title=str(_require(entry, "title", where)),
                description=str(_require(entry, "description", where)),
                relevance=int(_require(entry, "relevance", where)),
                provenance=provenance,
                company=str(entry.get("company", "")),
                location=str(entry.get("location", "Remote")),
                note=str(entry.get("note", "")),
                tags=tuple(str(t) for t in entry.get("tags", ())),
            )
        )

    if not items:
        raise ValueError(f"{location}: no postings")

    return LabeledSet(
        name=str(_require(raw, "name", str(location))),
        version=str(_require(raw, "version", str(location))),
        profile_text=str(_require(raw, "profile_text", str(location))),
        items=tuple(items),
        description=str(raw.get("description", "")),
    )
