"""The tracker's columns must be `Outcome` values, because the API sends those.

Found by screenshotting the dashboard, not by reading it. The board showed a
heading, a paragraph and nothing else while the database held two rejected
applications.

Two of the five column keys were `Classification` values — what an inbound
*email* is — rather than `Outcome` values, which is what lands on the
application:

    column key        Outcome value      matched?
    rejection         rejected           never
    info_request      info_requested     never

and `assessment`, `acknowledged` and `awaiting` had no column at all. So five
of the seven outcomes were unreachable, including the one CLAUDE.md §15 argues
is the most time-critical: an assessment is an opportunity with a deadline.

It did not look broken, which is why it survived. `columnFor` returned a
non-null key, so the "nothing submitted yet" empty state did not fire either —
the page rendered an empty div between the header and the footer.

These tests read the TSX and the enum rather than a copy of either. A list of
expected strings here would be a third place for the same drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from packages.core.enums import Classification, Outcome

TRACKER = Path(__file__).resolve().parent.parent / "apps/web/src/app/tracker/page.tsx"
SOURCE = TRACKER.read_text(encoding="utf-8")

#: The `key:` of each entry in the COLUMNS array.
COLUMN_BLOCK = SOURCE.split("const COLUMNS", 1)[1].split("];", 1)[0]
COLUMN_KEYS = set(re.findall(r'key:\s*"([a-z_]+)"', COLUMN_BLOCK))


def test_the_columns_were_found() -> None:
    """A regex that matched nothing would make every test below vacuous."""
    assert len(COLUMN_KEYS) >= 5, COLUMN_KEYS


@pytest.mark.parametrize("outcome", sorted(o.value for o in Outcome))
def test_every_outcome_has_a_column(outcome: str) -> None:
    """An outcome with no column is an application that vanishes from the board."""
    assert outcome in COLUMN_KEYS, (
        f"{outcome!r} is an Outcome the API can return and the tracker has no column "
        f"for it; it will not appear on the board. Columns: {sorted(COLUMN_KEYS)}"
    )


def test_no_column_is_keyed_on_a_classification_instead() -> None:
    """The exact confusion that caused this: two enums, overlapping names.

    `Classification` describes an inbound email; `Outcome` describes the
    application. A key that is one and not the other never matches anything.
    """
    outcomes = {o.value for o in Outcome}
    wrong = {k for k in COLUMN_KEYS if k in {c.value for c in Classification}} - outcomes
    assert not wrong, (
        f"{sorted(wrong)} are Classification values, not Outcome values — the API "
        f"never sends them on an application, so those columns stay empty forever"
    )


def test_an_outcome_with_no_column_is_still_shown() -> None:
    """The general form of the bug, so the next added value cannot vanish.

    A column nobody styled is a far smaller problem than an application nobody
    can see, and the empty state cannot be relied on to catch it — `tracked`
    counts the application either way.
    """
    assert "columnsFor" in SOURCE, "the fallback that surfaces an unnamed outcome is gone"
    assert "no column defined for this outcome yet" in SOURCE


def test_sent_and_silent_shares_the_waiting_column() -> None:
    """`columnFor` synthesises a key for submitted-with-no-outcome. It has to be
    the enum's own `awaiting`, or the board grows a second Waiting column that
    means the same thing."""
    assert '"awaiting"' in SOURCE
    assert re.search(r'return application\.status === "submitted" \? "awaiting"', SOURCE)
