"""Recognising each ATS's board document, empty or not.

The sweep that produced this file retired eight live companies — Palantir and
Spotify among them — because `_api_is_board` tested Greenhouse's
`{"jobs": [...]}` shape against every ATS, and Lever answers with a bare array.
A live board returning 200 with its full posting list failed the shape test,
fell through to MISSING, and `--write` moved it to `retired:`.

Every entry it condemned was stamped `api_status: 200`, which is the tell: no
dead board returns 200. That is what these tests are for — the failure was not
that a check errored, it was that a check was confidently wrong in the
direction that removes data.
"""

from __future__ import annotations

import json

import pytest

from packages.crawler.extract import EXTRACTORS
from packages.crawler.fetch import FetchResult, content_hash
from packages.crawler.validate import UnknownBoardShape, _api_is_board

#: A minimal body in each ATS's real board shape, carrying one posting. Keyed
#: by the same names `EXTRACTORS` uses so the parametrised test below can
#: assert the two sets agree.
BODIES: dict[str, str] = {
    "greenhouse": json.dumps({"jobs": [{"id": 1, "title": "Engineer"}]}),
    "ashby": json.dumps({"jobs": [{"id": "a", "title": "Engineer"}]}),
    "workable": json.dumps({"jobs": [{"shortcode": "AB12", "title": "Engineer"}]}),
    # The one that caused the damage: a bare array, no envelope.
    "lever": json.dumps([{"id": "x", "text": "Engineer"}]),
}

#: The same four with nothing currently open. A live board hiring slowly.
EMPTY_BODIES: dict[str, str] = {
    "greenhouse": json.dumps({"jobs": []}),
    "ashby": json.dumps({"jobs": []}),
    "workable": json.dumps({"jobs": []}),
    "lever": json.dumps([]),
}


def _response(body: str, status: int = 200) -> FetchResult:
    return FetchResult(
        url="https://example.test/board",
        status=status,
        text=body,
        content_hash=content_hash(body),
    )


@pytest.mark.parametrize("ats", sorted(BODIES))
def test_a_live_board_is_recognised_for_every_ats(ats: str) -> None:
    assert _api_is_board(_response(BODIES[ats]), ats) is True


@pytest.mark.parametrize("ats", sorted(EMPTY_BODIES))
def test_a_board_with_nothing_open_is_still_a_board(ats: str) -> None:
    """Shape, not posting count.

    Counting postings would retire a company for having nothing open this
    week, which is a hiring pace rather than a departure.
    """
    assert _api_is_board(_response(EMPTY_BODIES[ats]), ats) is True


def test_the_lever_regression_specifically() -> None:
    """The exact body shape that condemned Palantir, asserted on its own.

    Named rather than left to the parametrised sweep above so a regression
    reports the company it would remove, not an index.
    """
    body = json.dumps([{"id": "abc", "text": "Forward Deployed Engineer"}])

    assert _api_is_board(_response(body), "lever") is True
    assert _api_is_board(_response(body), "greenhouse") is False, (
        "and the shapes are genuinely distinct — this is not a check that passes everything"
    )


@pytest.mark.parametrize("ats", sorted(BODIES))
def test_a_404_is_not_a_board_whatever_the_body(ats: str) -> None:
    assert _api_is_board(_response(BODIES[ats], status=404), ats) is False


@pytest.mark.parametrize("ats", sorted(BODIES))
def test_a_non_json_body_is_not_a_board(ats: str) -> None:
    assert _api_is_board(_response("<html>Board not found</html>"), ats) is False


def test_the_wrong_container_is_not_a_board() -> None:
    """An envelope where an array belongs, and the reverse."""
    assert _api_is_board(_response(json.dumps({"jobs": []})), "lever") is False
    assert _api_is_board(_response(json.dumps([])), "greenhouse") is False


def test_every_extractor_has_a_known_board_shape() -> None:
    """The guard against this recurring when P12's five adapters land.

    A new extractor with no shape entry used to mean "not a board", which
    means "this company left", which `--write` acts on. Now it raises, and
    this test fails first — at the point the adapter is added, not on the
    owner's next sweep.
    """
    for ats in EXTRACTORS:
        assert ats in BODIES, f"{ats} has an extractor but no board-shape fixture here"
        assert _api_is_board(_response(BODIES[ats]), ats) is True


def test_an_unregistered_ats_raises_rather_than_condemning() -> None:
    with pytest.raises(UnknownBoardShape):
        _api_is_board(_response(json.dumps({"jobs": []})), "smartrecruiters")
