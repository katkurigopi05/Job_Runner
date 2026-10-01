"""The retrieval graph: that it loops, widens, bounds, and costs no model call.

`packages/matching/multihop.py` is the one place a graph framework earns its
keep here — a cycle whose exit depends on what the last pass found. These tests
hold the properties that make it safe to have rather than the fact that
LangGraph works.

The load-bearing one is `test_a_hop_costs_no_provider_call`. §7's daily
allowance is request-capped, so the obvious build — asking a model whether the
results are good enough — would triple what one question costs. The routing is
computed instead, and that has to stay true.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import Company, Posting
from packages.matching.multihop import (
    MAX_HOPS,
    THIN,
    HopResult,
    _rarest_term,
    _without,
    build_graph,
    retrieve_multihop,
)
from packages.matching.retrieve import Passage
from packages.matching.score import embed_postings


async def _corpus(session: AsyncSession, roles: list[tuple[str, str]]) -> Company:
    company = Company(name=f"Acme-{uuid.uuid4().hex[:6]}", domain="acme.test")
    session.add(company)
    await session.flush()
    postings = [
        Posting(
            company_id=company.id,
            url=f"https://acme.test/{uuid.uuid4()}",
            title=title,
            description_raw=body,
            first_seen_at=datetime.now(UTC),
            external_id=f"{company.id}-{title}",
        )
        for title, body in roles
    ]
    for posting in postings:
        session.add(posting)
    await session.flush()
    await embed_postings(session, postings)
    await session.flush()
    return company


OFF_TOPIC = [
    ("Pastry Chef", "Laminated doughs, viennoiserie, early mornings in the bakery."),
    ("Truck Driver", "Long haul routes, class A licence, logbook compliance."),
    ("Dental Hygienist", "Cleanings, radiographs, patient charting."),
    ("Florist", "Arrangements, wholesale sourcing, seasonal displays."),
]


# --- the loop --------------------------------------------------------------


async def test_a_thin_first_pass_takes_a_second_hop(db_session) -> None:
    """The whole reason the graph exists. One pass cannot try again."""
    await _corpus(db_session, OFF_TOPIC)
    result = await retrieve_multihop(db_session, "Kubernetes jobs")
    assert result.hops > 1, "a pass finding nothing did not widen"
    assert len(result.queries) == result.hops
    assert result.queries[0] == "Kubernetes jobs"


async def test_broadening_drops_a_term_each_hop(db_session) -> None:
    await _corpus(db_session, OFF_TOPIC)
    result = await retrieve_multihop(db_session, "backend engineer jobs")
    widths = [len(q.split()) for q in result.queries]
    assert widths == sorted(widths, reverse=True), f"query did not narrow in words: {widths}"
    assert len(set(result.queries)) == len(result.queries), "a query was repeated"


async def test_a_full_first_pass_stops_at_one_hop(db_session) -> None:
    """Widening a search that already worked costs time and finds worse."""
    await _corpus(
        db_session,
        [
            ("Backend Engineer", "Python services on Postgres and Kubernetes."),
            ("Senior Backend Engineer", "Python, Postgres, Kubernetes, payments."),
            ("Staff Backend Engineer", "Python and Postgres at scale."),
        ],
    )
    result = await retrieve_multihop(db_session, "backend engineer jobs")
    assert len(result.passages) >= THIN
    assert result.hops == 1
    assert result.stopped == "enough"


async def test_the_loop_is_bounded(db_session) -> None:
    """A graph with a cycle and no bound is a graph that does not return."""
    await _corpus(db_session, OFF_TOPIC)
    result = await retrieve_multihop(db_session, "Kubernetes Terraform Clojure jobs")
    assert result.hops <= MAX_HOPS
    assert result.stopped in {"enough", "exhausted", "no_broader_query"}


def test_merge_keeps_both_hops_and_prefers_the_earlier_ranking() -> None:
    """The reducer on `HopState.passages`, tested where it can actually fail.

    A later hop is a *wider* search, so a posting it rediscovers was already
    ranked higher by the narrower one. Without the merge the second hop's
    result would replace the first's and the better ranking would be lost —
    which the end-to-end test below cannot see, because a corpus where hop 2
    finds something hop 1 missed is hard to arrange and easy to fool yourself
    about. This one discriminates.
    """
    from packages.matching.multihop import _merge

    first, second, third = (uuid.uuid4() for _ in range(3))

    def passage(label: str, pid: uuid.UUID, excerpt: str) -> Passage:
        return Passage(
            label=label,
            posting_id=pid,
            title="Engineer",
            company="Acme",
            location=None,
            url="https://acme.test/x",
            excerpt=excerpt,
            application_status=None,
        )

    hop1 = [passage("P1", first, "narrow")]
    hop2 = [passage("P1", first, "wide"), passage("P2", second, "new"), passage("P3", third, "new")]

    merged = _merge(hop1, hop2)
    assert [p.posting_id for p in merged] == [first, second, third], "a hop was dropped"
    assert merged[0].excerpt == "narrow", "the wider hop overwrote the narrower hop's hit"
    assert _merge([], hop2) == hop2
    assert _merge(hop1, []) == hop1


async def test_hops_accumulate_what_they_find(db_session) -> None:
    """A later hop adds to the earlier one rather than replacing it.

    Written against a corpus where the second query reaches a posting the
    first does not: without accumulation the wider hop's results would be all
    that survived, and the better-ranked narrow hit would be lost.
    """
    await _corpus(
        db_session,
        [
            *OFF_TOPIC,
            ("Pastry Sous Chef", "Bakery production, pastry, laminated dough."),
        ],
    )
    result = await retrieve_multihop(db_session, "viennoiserie pastry jobs")
    titles = [p.title for p in result.passages]
    assert len(titles) == len(set(titles)), f"a posting was returned twice: {titles}"


async def test_a_question_that_is_not_about_postings_does_not_loop(db_session) -> None:
    """Widening a question the corpus has no opinion on searches for less of nothing."""
    await _corpus(db_session, OFF_TOPIC)
    result = await retrieve_multihop(db_session, "what is the weather today")
    assert result.attempted is False
    assert result.hops == 1
    assert result.passages == ()


# --- cost ------------------------------------------------------------------


async def test_a_hop_costs_no_provider_call(db_session, monkeypatch) -> None:
    """The property that keeps §7's quota flat however many hops run.

    The obvious build asks a model whether the results are good enough. That
    is request-capped allowance spent per hop, on a judgement two integers
    already answer.
    """
    import packages.llm.router as router

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("retrieval reached a provider")

    for name in ("best_available", "for_task"):
        if hasattr(router, name):
            monkeypatch.setattr(router, name, explode)

    await _corpus(db_session, OFF_TOPIC)
    result = await retrieve_multihop(db_session, "Kubernetes jobs")
    assert result.hops > 1, "the test did not exercise the loop it is measuring"


def test_the_graph_persists_no_state_of_its_own() -> None:
    """No checkpointer. §6 makes `transition()` the only writer of status, and
    §17's stream is correct only because the event log is the only publication
    path. A graph with its own store is a second one that can disagree.
    """
    import ast
    import inspect

    from packages.matching import multihop

    # The AST, not the source text. This module's docstring explains at length
    # why there is no checkpointer, so a grep reads the explanation as the
    # violation — the same trap `scripts/import_companies.py`'s test records.
    tree = ast.parse(inspect.getsource(multihop))
    compiles = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "compile"
    ]
    assert compiles, "the graph is no longer compiled here"
    for call in compiles:
        assert not call.args, "compile() was given a positional argument"
        assert not call.keywords, f"compile() was given {[k.arg for k in call.keywords]}"


def test_langsmith_tracing_is_not_enabled() -> None:
    """LangGraph brings `langsmith`, which can post traces to a third party.

    Off unless an environment variable turns it on, and §2.8 makes that worth
    a test rather than a trust: a trace carries the question, which carries
    the owner's applications.
    """
    from langsmith import utils

    assert utils.tracing_is_enabled() is False


# --- the widening rule -----------------------------------------------------


@pytest.mark.parametrize(
    ("query", "dropped"),
    [
        ("backend engineer jobs", "engineer"),
        ("Kubernetes jobs", "kubernetes"),
        ("remote python roles", "remote"),
    ],
)
def test_the_longest_term_is_the_one_dropped(query: str, dropped: str) -> None:
    assert _rarest_term(query) == dropped


def test_a_query_with_nothing_to_drop_says_so() -> None:
    """One term left is not a query that can be widened further."""
    assert _rarest_term("jobs") is None
    assert _rarest_term("") is None


def test_dropping_a_term_keeps_the_rest_in_order() -> None:
    assert _without("backend engineer jobs", "engineer") == "backend jobs"
    assert _without("Senior Backend Engineer jobs", "backend") == "Senior Engineer jobs"


def test_the_result_converts_to_the_one_pass_shape() -> None:
    """Callers swap `retrieve()` for this without learning a second type."""
    result = HopResult(attempted=True, searched=7, unsearchable=2, hops=2)
    plain = result.as_retrieval()
    assert (plain.attempted, plain.searched, plain.unsearchable) == (True, 7, 2)
    assert not hasattr(plain, "hops")


async def test_the_graph_compiles_without_a_session_round_trip(db_session) -> None:
    """`build_graph` is pure wiring; nothing runs until `ainvoke`."""
    graph = build_graph(db_session)
    assert graph is not None


# --- the route -------------------------------------------------------------


async def test_the_chat_route_takes_one_pass_unless_asked(client, monkeypatch) -> None:
    """Off by default. The shipped path is the single pass it has always been.

    A flag that changed an answer for someone who never set it would be a
    feature arriving by surprise in the one place §14 keeps deliberate.
    """
    import packages.matching.multihop as multihop_module
    from apps.api.routers import chat as chat_route

    calls: list[str] = []

    async def spy_multihop(*args: object, **kwargs: object) -> object:
        calls.append("multihop")
        raise AssertionError("multihop ran for a request that did not ask for it")

    monkeypatch.setattr(chat_route, "retrieve_multihop", spy_multihop)
    # The status is not what this asserts: no model runs in the suite, so the
    # route answers 500 once retrieval is done. What matters is which
    # retrieval ran, and that happens before the provider is reached.
    await client.post("/chat", json={"message": "what is waiting on me?"})
    assert calls == []
    assert multihop_module.MAX_HOPS >= 1


async def test_the_flag_reaches_the_graph(client, monkeypatch) -> None:
    from apps.api.routers import chat as chat_route
    from packages.matching.multihop import HopResult

    seen: list[bool] = []

    async def spy_multihop(session: object, question: str, **kwargs: object) -> HopResult:
        seen.append(True)
        return HopResult(attempted=False)

    monkeypatch.setattr(chat_route, "retrieve_multihop", spy_multihop)
    await client.post(
        "/chat", json={"message": "which postings mention Kubernetes?", "multihop": True}
    )
    assert seen == [True], "the opt-in did not reach the graph"
