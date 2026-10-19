"""Grading the assistant's search on a frozen question set: the pool.

Phase 5, part 2, of `docs/HARNESS_AUDIT_2026-10-15.md`: owner relevance on a
frozen set, before any change to how the search ranks. The known-target
benchmark (`known_target.py`) asks where one generated posting lands. This
asks the owner which of the postings a search returned are what was asked.

The questions are `seeds/chat_judgment_questions.json`, mostly in the owner's
own words. Each is asked once per search mode, and the postings any mode
returned, to the set's depth, are pooled into `retrieval_judgments` for the
owner to grade. Pooling every mode is what lets part 3 compare them: a mode
measured against grades another mode's results produced would score its own
new finds as misses.

Three things are load-bearing:

- **A mode that names a model it cannot load is refused**, not run as the
  shipped search under another name.
- **The pool is written once and only added to.** A grade is never touched
  by building it again, and `surfaced_by` keeps the ranks it was built from.
- **The order the owner sees is a stable shuffle**, and which mode found a
  posting is never shown, so no mode's order leans on a grade.

Questions expected to find nothing, and ones that are not a search at all,
are pooled with nothing: what they are scored on is whether the search said
"none", or did not search.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config import Settings, get_settings
from packages.core.models import Posting
from packages.core.models_judgments import RetrievalJudgment
from packages.matching import retrieve as search
from packages.matching.rerank import Reranker, load_reranker

DEFAULT_SET = Path("seeds/chat_judgment_questions.json")
#: Share of questions held out from every choice of rule or threshold, by a
#: stable hash of the key, as `evaluation.HOLDOUT_PERCENT` does for postings.
HOLDOUT_PERCENT = 30

Expect = Literal["answer", "none", "not_searched"]
_EXPECTS: frozenset[str] = frozenset({"answer", "none", "not_searched"})


def is_held_out(key: str) -> bool:
    digest = hashlib.sha256(key.encode()).digest()
    return digest[0] * 100 // 256 < HOLDOUT_PERCENT


@dataclass(frozen=True)
class Question:
    key: str
    question: str
    stratum: str
    expect: Expect

    @property
    def held_out(self) -> bool:
        return is_held_out(self.key)


@dataclass(frozen=True)
class Mode:
    """A search mode: the switches it sets over the settings in force."""

    name: str
    settings: dict[str, Any]


@dataclass(frozen=True)
class QuestionSet:
    version: str
    depth: int
    modes: tuple[Mode, ...]
    questions: tuple[Question, ...]


def load(path: Path = DEFAULT_SET) -> QuestionSet:
    """The frozen set, refused if anything in it could be misread.

    A switch name that `Settings` does not have is refused rather than
    ignored: a typo would run the shipped search and report it as the mode.
    """
    raw = json.loads(path.read_text())
    version = str(raw["version"])
    depth = int(raw["depth"])
    if not 0 < len(version) <= 32:
        raise ValueError(f"set version {version!r} must be 1 to 32 characters")
    if depth <= 0:
        raise ValueError("depth must be positive")
    known = set(Settings.model_fields)
    modes = []
    for name, switches in raw["modes"].items():
        unknown = set(switches) - known
        if unknown:
            raise ValueError(f"mode {name!r} sets unknown settings {sorted(unknown)}")
        modes.append(Mode(name=str(name), settings=dict(switches)))
    questions = []
    for entry in raw["questions"]:
        if entry["expect"] not in _EXPECTS:
            raise ValueError(f"question {entry['key']!r} expects {entry['expect']!r}")
        questions.append(
            Question(
                key=str(entry["key"]),
                question=str(entry["question"]),
                stratum=str(entry["stratum"]),
                expect=cast(Expect, entry["expect"]),
            )
        )
    keys = [q.key for q in questions]
    if len(keys) != len(set(keys)):
        raise ValueError("question keys must be unique")
    if any(not 0 < len(q.key) <= 64 or len(q.stratum) > 32 for q in questions):
        raise ValueError("a key is 1 to 64 characters and a stratum at most 32")
    return QuestionSet(version=version, depth=depth, modes=tuple(modes), questions=tuple(questions))


_loaded: dict[str, Reranker] = {}


def _reranker_for(name: str) -> Reranker | None:
    name = name.strip()
    if not name:
        return None
    if name not in _loaded:
        reranker = load_reranker(name)
        if reranker is None:
            raise RuntimeError(f"re-ranker {name!r} could not be loaded, so the mode is not itself")
        _loaded[name] = reranker
    return _loaded[name]


@contextmanager
def searching_as(mode: Mode) -> Iterator[None]:
    """`retrieve` as `mode` searches, for one process running an evaluation.

    The two names `retrieve` reads its switches through are swapped for the
    duration and put back. Not for the API, which serves requests at once:
    this is one process asking one question at a time.
    """
    settings = get_settings().model_copy(update=mode.settings)
    reranker = _reranker_for(settings.chat_rerank_model)
    names = vars(search)
    saved = (names["get_settings"], names["get_reranker"])
    setattr(search, "get_settings", lambda: settings)  # noqa: B010
    setattr(search, "get_reranker", lambda: reranker)  # noqa: B010
    try:
        yield
    finally:
        setattr(search, "get_settings", saved[0])  # noqa: B010
        setattr(search, "get_reranker", saved[1])  # noqa: B010


@dataclass(frozen=True)
class Asked:
    """What one mode returned for one question."""

    attempted: bool
    ranked: tuple[uuid.UUID, ...]


async def ask(session: AsyncSession, question: str, depth: int) -> Asked:
    """The postings the search returned, shown first, then listed, to `depth`."""
    found = await search.retrieve(session, question)
    ids = [p.posting_id for p in found.passages] + [m.posting_id for m in found.more]
    return Asked(attempted=found.attempted, ranked=tuple(dict.fromkeys(ids))[:depth])


@dataclass
class Pool:
    version: str
    #: `(question key, mode) -> what it returned`.
    asked: dict[tuple[str, str], Asked] = field(default_factory=dict)
    #: `(question key, posting id) -> {mode: rank}`, for questions expecting an answer.
    surfaced: dict[tuple[str, uuid.UUID], dict[str, int]] = field(default_factory=dict)

    def per_question(self, key: str) -> int:
        return sum(1 for question_key, _ in self.surfaced if question_key == key)


async def build_pool(session: AsyncSession, questions: QuestionSet) -> Pool:
    """Ask every question in every mode. Reads only."""
    pool = Pool(version=questions.version)
    for mode in questions.modes:
        with searching_as(mode):
            for question in questions.questions:
                asked = await ask(session, question.question, questions.depth)
                pool.asked[(question.key, mode.name)] = asked
                if question.expect != "answer":
                    continue
                for rank, posting_id in enumerate(asked.ranked, start=1):
                    pool.surfaced.setdefault((question.key, posting_id), {})[mode.name] = rank
    return pool


def pool_order(version: str, key: str, posting_id: uuid.UUID) -> int:
    """Where a posting is shown among its question's: a stable shuffle."""
    digest = hashlib.sha256(f"{version}:{key}:{posting_id}".encode()).digest()
    return int.from_bytes(digest[:3], "big")


async def write_pool(session: AsyncSession, questions: QuestionSet, pool: Pool) -> int:
    """Add what is not pooled yet. Never touches a row already there. Does not commit."""
    if not pool.surfaced:
        return 0
    found = await session.execute(
        select(Posting.id, Posting.content_hash).where(
            Posting.id.in_({posting_id for _, posting_id in pool.surfaced})
        )
    )
    hashes: dict[uuid.UUID, str | None] = {posting_id: digest for posting_id, digest in found}
    by_key = {q.key: q for q in questions.questions}
    rows = [
        {
            "set_version": questions.version,
            "question_key": key,
            "question": by_key[key].question,
            "stratum": by_key[key].stratum,
            "held_out": by_key[key].held_out,
            "posting_id": posting_id,
            "pool_order": pool_order(questions.version, key, posting_id),
            "surfaced_by": surfaced,
            "content_hash": hashes.get(posting_id),
        }
        for (key, posting_id), surfaced in pool.surfaced.items()
        # A posting deleted since it was asked about has nothing to grade.
        if posting_id in hashes
    ]
    if not rows:
        return 0
    statement = (
        insert(RetrievalJudgment)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_retrieval_judgments_question_posting")
        .returning(RetrievalJudgment.id)
    )
    return len((await session.scalars(statement)).all())


__all__ = [
    "DEFAULT_SET",
    "HOLDOUT_PERCENT",
    "Asked",
    "Mode",
    "Pool",
    "Question",
    "QuestionSet",
    "ask",
    "build_pool",
    "is_held_out",
    "load",
    "pool_order",
    "searching_as",
    "write_pool",
]
