"""Multi-hop retrieval, as a LangGraph graph.

`retrieve.retrieve` is one pass: read the question, search, return. When it
comes back thin there is nothing that tries again with a wider net, and a
question whose distinguishing term appears in no posting gets a short context
and an answer grounded in very little.

This is the one shape the straight-line path cannot express — a cycle whose
exit depends on what the previous pass found. The graph is three nodes:

```text
    search ──> assess ──(thin, hops left)──> broaden ──┐
                  │                                     │
                  └──(enough, or out of hops)──> END    │
                  ▲                                     │
                  └─────────────────────────────────────┘
```

## The routing decision is computed, never asked of a model

The obvious build asks the model "are these results sufficient?" and branches
on the answer. That is refused here for a reason with a number attached:
§7's daily allowance is **request**-capped, not token-capped, so a model call
per hop triples the quota a single question costs — and §14 keeps the
assistant grounded rather than freehand, which an agent deciding its own
search terms moves away from.

So `assess` reads counts, and `broaden` widens the query by dropping its
rarest term — the one most likely to be the reason nothing matched. Both are
deterministic. The graph manages control flow; it does not add a judge.

**This means hops cost no provider calls at all.** The only model call is the
answer, same as before, and `packages/llm/audit.py` records exactly what it
recorded for a one-pass question.

## No checkpointer, deliberately

`StateGraph.compile()` takes one and this passes none. CLAUDE.md §6 makes
`core/state.py::transition()` the single place an application's status
changes, and §17's stream is correct only because the event log is the single
publication path — "the stream cannot disagree with the database" is a
property of there being one writer. A graph persisting its own state would be
a second store that can disagree with the first, which is §6's bug
institutionalised rather than introduced by accident.

Retrieval is a read. It has nothing worth resuming: a dropped hop costs
milliseconds, and re-running it is cheaper than storing it.

## What it does not touch

The apply pipeline. `apps/worker/apply_job.py` is a state machine with parks
that a human resumes, and it already has the thing a graph framework is for.
Nothing here imports it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from packages.matching.retrieve import DEFAULT_LIMIT, Passage, Retrieval, retrieve

log = structlog.get_logger(__name__)

#: Most passes over the corpus for one question. Two is the default because a
#: third has nothing new to drop: `broaden` removes one term per hop, and a
#: question carries few distinguishing terms to begin with.
MAX_HOPS = 2

#: Below this many passages a hop is "thin" and worth widening. Not zero: one
#: passage is a context the model will answer from as though it were the whole
#: corpus, which is the failure a second hop exists to prevent.
THIN = 2


def _merge(left: list[Passage], right: list[Passage]) -> list[Passage]:
    """Accumulate passages across hops, first occurrence winning.

    A later hop is a *wider* search, so a posting it rediscovers was already
    ranked higher by the narrower one. Keeping the first keeps the better
    ranking, and keeps hop order out of the answer's citations.
    """
    seen = {p.posting_id for p in left}
    return left + [p for p in right if p.posting_id not in seen]


class HopState(TypedDict, total=False):
    """What travels between nodes. Flat on purpose — it is read in tests."""

    question: str
    #: The query actually searched this hop. Starts as the question and loses
    #: a term each time `broaden` runs.
    query: str
    hops: int
    passages: Annotated[list[Passage], _merge]
    searched: int
    unsearchable: int
    attempted: bool
    #: Every query tried, in order. The audit trail for a widened search: an
    #: answer drawn from a broadened query is drawn from a different question
    #: than the one asked, and the owner should be able to see that.
    queries: list[str]
    stopped: str


@dataclass(frozen=True)
class HopResult:
    """The graph's outcome, in the shape `retrieve.Retrieval` already has.

    Carries `Retrieval`'s fields so a caller can swap one for the other, plus
    the two facts only a multi-hop search has: how many passes it took and
    what it searched for on each.
    """

    attempted: bool
    passages: tuple[Passage, ...] = ()
    searched: int = 0
    unsearchable: int = 0
    hops: int = 0
    queries: tuple[str, ...] = ()
    #: Why the loop ended: "enough", "exhausted" (out of hops),
    #: "no_broader_query" (nothing left to drop), or
    #: "not_a_posting_question" (nothing was searched in the first place).
    stopped: str = "enough"

    def as_retrieval(self) -> Retrieval:
        return Retrieval(
            attempted=self.attempted,
            passages=self.passages,
            searched=self.searched,
            unsearchable=self.unsearchable,
        )


def _rarest_term(query: str) -> str | None:
    """The term to drop when widening. None when there is nothing to drop.

    Longest word wins, which is a crude proxy for rarest and a deliberate one:
    the alternative is a corpus frequency lookup per hop, and the term that
    makes a question fail to match is usually a specific one — a product name,
    a niche technology — which is usually the long one. A proxy that costs a
    database round trip would have to be better than this to be worth it, and
    nothing here has measured that it would be.
    """
    from packages.matching.embed import tokenize

    terms = tokenize(query)
    if len(terms) < 2:
        return None
    return max(terms, key=len)


def _without(query: str, term: str) -> str:
    kept = [w for w in query.split() if term not in w.lower()]
    return " ".join(kept)


def build_graph(session: AsyncSession, *, limit: int = DEFAULT_LIMIT) -> Any:
    """Compile the retrieval graph. No checkpointer — see the module docstring."""

    async def search(state: HopState) -> HopState:
        found = await retrieve(session, state["query"], limit=limit)
        return {
            "attempted": state.get("attempted", False) or found.attempted,
            "passages": list(found.passages),
            # The corpus counts describe the corpus, not the query, so the
            # widest hop's view is the honest one rather than the sum.
            "searched": max(state.get("searched", 0), found.searched),
            "unsearchable": max(state.get("unsearchable", 0), found.unsearchable),
            "queries": [*state.get("queries", []), state["query"]],
            "hops": state.get("hops", 0) + 1,
        }

    def assess(state: HopState) -> HopState:
        # Nothing was searched, so there is nothing to widen. `retrieve`
        # returns this when the question was not about postings at all, and a
        # broader version of such a question is still not about postings — it
        # is just a shorter string costing another pass over the corpus.
        if not state.get("attempted"):
            return {"stopped": "not_a_posting_question"}
        if len(state.get("passages", [])) >= THIN:
            return {"stopped": "enough"}
        if state.get("hops", 0) >= MAX_HOPS:
            return {"stopped": "exhausted"}
        if _rarest_term(state["query"]) is None:
            return {"stopped": "no_broader_query"}
        return {"stopped": ""}

    def route(state: HopState) -> Literal["broaden", "__end__"]:
        # END is the literal "__end__"; naming it keeps the edge map readable.
        return "broaden" if not state.get("stopped") else "__end__"

    def broaden(state: HopState) -> HopState:
        term = _rarest_term(state["query"])
        assert term is not None  # `assess` routed here only when one exists
        widened = _without(state["query"], term)
        log.info("retrieval_broadened", dropped=term, hop=state.get("hops", 0))
        return {"query": widened}

    graph = StateGraph(HopState)
    graph.add_node("search", search)
    graph.add_node("assess", assess)
    graph.add_node("broaden", broaden)
    graph.add_edge(START, "search")
    graph.add_edge("search", "assess")
    graph.add_conditional_edges("assess", route, {"broaden": "broaden", END: END})
    graph.add_edge("broaden", "search")
    return graph.compile()


async def retrieve_multihop(
    session: AsyncSession, question: str, *, limit: int = DEFAULT_LIMIT
) -> HopResult:
    """`retrieve.retrieve`, widening the query when a pass comes back thin.

    A question that was never about postings returns `attempted=False` on the
    first hop and stops, exactly as the one-pass path does — widening a
    question the corpus has no opinion on would search for less of nothing.
    """
    final: HopState = await build_graph(session, limit=limit).ainvoke(
        {"question": question, "query": question, "hops": 0, "passages": [], "queries": []}
    )
    return HopResult(
        attempted=bool(final.get("attempted")),
        passages=tuple(final.get("passages", []))[:limit],
        searched=final.get("searched", 0),
        unsearchable=final.get("unsearchable", 0),
        hops=final.get("hops", 0),
        queries=tuple(final.get("queries", [])),
        stopped=final.get("stopped") or "enough",
    )


__all__ = [
    "MAX_HOPS",
    "THIN",
    "HopResult",
    "HopState",
    "build_graph",
    "retrieve_multihop",
]
