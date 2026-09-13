"""The feed has to show what you have, and rank rather than grade.

Both of these are one-line regressions away, and neither would fail anything
else. They are here because the owner's complaint was not that the dashboard
was ugly — it was that using it felt tense — and two mechanical choices were
most of the reason.

**It showed only what you lack.** `/matches` rendered `missing_terms` and
never rendered `matched_terms`, though the API sends both. So the feed's
answer to "how do I look for this job" was a list of eight things absent from
your résumé and nothing at all about the things on it.

**It graded you on an uncalibrated number.** The score is a cosine similarity.
Measured on the owner's own data: 74 matches spanning 0.000–0.081, against
profile thresholds of 0.75 and 0.0. A threshold-relative label therefore
collapses to a single value — every posting is a long shot under one profile
and a strong match under the other — and a feed of single-digit percentages
reads as "you are a poor fit for everything" when it means nothing of the
sort. Rank within the feed always spreads and cannot degenerate.
"""

from __future__ import annotations

import re
from pathlib import Path

FEED = Path(__file__).resolve().parent.parent / "apps/web/src/app/matches/page.tsx"
SOURCE = FEED.read_text(encoding="utf-8")


def test_the_feed_shows_what_you_already_have() -> None:
    """`matched_terms` reaches this screen. For a long time it did not."""
    assert "matched_terms" in SOURCE, (
        "the match feed renders no matched terms; it is showing the owner only "
        "what their résumé lacks, which is the API's other list"
    )


def test_fit_is_shown_before_the_gaps() -> None:
    """Order is the point. Both lists present, gaps first, is the old feeling."""
    assert SOURCE.index("matched_terms") < SOURCE.index("missing_terms"), (
        "missing terms come first on the card — lead with what the owner has"
    )


def test_the_verdict_is_rank_not_a_threshold_comparison() -> None:
    """A threshold label collapses on this data; see the module docstring."""
    signature = re.search(r"function verdict\(([^)]*)\)", SOURCE)
    assert signature, "the verdict helper is gone"
    params = signature.group(1)
    assert "rank" in params and "total" in params, (
        f"verdict({params}) is not rank-based; with scores at 0.000–0.081 and a "
        f"0.75 threshold every posting gets the same label"
    )


def test_the_score_is_labelled_as_a_similarity_not_a_grade() -> None:
    """`8%` alone invites reading a cosine as a mark out of a hundred."""
    assert "% similar" in SOURCE
    assert "not a grade" in SOURCE, "the tooltip that says what the number is has gone"


def test_clearing_the_owners_own_threshold_is_still_reported() -> None:
    """Rank answers "compared to these"; the threshold answers "compared to
    what I asked for". Dropping the second to fix the first would lose the
    only number the owner actually set."""
    assert "above your threshold" in SOURCE
