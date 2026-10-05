"""`make crawl`'s flags become the payload the worker reads.

`max_backlog` is here because queueing the owner's whole sheet at once is the
point of the directory work: at the default 2,000 a 3,800-company sweep stalls
behind its own backlog until half of it drains, a tick at a time.
"""

from __future__ import annotations

import pytest

from scripts.crawl import payload_from


def test_max_backlog_reaches_the_payload() -> None:
    payload = payload_from(["--dispatch", "--max-backlog", "4000"])

    assert payload == {"dispatch": True, "max_backlog": 4000}


def test_the_default_leaves_the_setting_in_charge() -> None:
    assert "max_backlog" not in payload_from(["--dispatch"])


def test_max_backlog_requires_dispatch() -> None:
    """The seed-file cycle has no backlog; accepting it there would be a no-op."""
    with pytest.raises(SystemExit):
        payload_from(["--max-backlog", "4000"])


@pytest.mark.parametrize("value", ["0", "-5"])
def test_a_backlog_that_dispatches_nothing_is_refused(value: str) -> None:
    with pytest.raises(SystemExit):
        payload_from(["--dispatch", "--max-backlog", value])


def test_the_pilot_flags_are_unchanged() -> None:
    assert payload_from(["--dispatch", "--limit", "20", "--once"]) == {
        "dispatch": True,
        "limit": 20,
        "repeat": False,
    }
