"""A vocabulary the dashboard names is the one the API sends.

`apps/web/src/lib/api.ts` declares some of the API's enums a second time, as
TypeScript unions, and nothing compared the two. `Classification.ASSESSMENT`
was added to the Python enum (CLAUDE.md §15: an assessment is the one inbound
message with a clock on it) and never to the union, so TypeScript accepted a
`Record<Classification, string>` of labels with no entry for it, and a tracker
card whose latest message was an assessment read "read as undefined".

`tests/test_tracker_columns.py` holds the board's columns against `Outcome`
for the same reason, after the same kind of drift emptied the board. This
holds every union that carries an enum's name, so the next one is caught by
its name alone. The idea of one authoritative vocabulary with every reader
held to it is `tests/test_tracker_status_vocab.py` in
MadsLorentzen/ai-job-search (MIT).

Both sides are read from their sources. A list of expected strings here would
be a third place to drift.
"""

from __future__ import annotations

import enum
import re
from pathlib import Path

import pytest

from packages.core import enums

WEB = Path(__file__).resolve().parent.parent / "apps/web/src"
API_TS = (WEB / "lib/api.ts").read_text(encoding="utf-8")
TRACKER = (WEB / "app/tracker/page.tsx").read_text(encoding="utf-8")

#: `export type Name = …;`, up to the semicolon that ends it.
_DECLARATION = re.compile(r"export type (\w+) =([^;]*);")
_LITERAL = re.compile(r"\"([^\"]*)\"")


def _string_unions(source: str) -> dict[str, set[str]]:
    """Each type that is nothing but string literals joined by `|`.

    Read in two steps, a declaration and then the literals in it, so no
    pattern repeats a group that can itself match in more than one way.
    """
    found: dict[str, set[str]] = {}
    for name, body in _DECLARATION.findall(source):
        members = _LITERAL.findall(body)
        if members and not _LITERAL.sub("", body).replace("|", "").strip():
            found[name] = set(members)
    return found


UNIONS: dict[str, set[str]] = _string_unions(API_TS)
ENUMS: dict[str, set[str]] = {
    name: {member.value for member in value}
    for name, value in vars(enums).items()
    if isinstance(value, type)
    and issubclass(value, enum.Enum)
    and value.__module__ == enums.__name__
}
SHARED = sorted(set(UNIONS) & set(ENUMS))


def test_the_unions_and_the_enums_were_found() -> None:
    """A regex that matched nothing would make the test below vacuous."""
    assert {"ApplicationStatus", "FailureReason", "Classification"} <= set(SHARED), SHARED


@pytest.mark.parametrize("name", SHARED)
def test_a_union_named_for_an_enum_has_its_values(name: str) -> None:
    missing = ENUMS[name] - UNIONS[name]
    invented = UNIONS[name] - ENUMS[name]

    assert not missing, (
        f"the API can send {sorted(missing)} as a {name} and `api.ts` does not list it, "
        "so a lookup keyed on the type has no entry for it"
    )
    assert not invented, f"`api.ts` lists {sorted(invented)} as a {name}; the API never sends it"


def test_every_classification_has_a_label_on_the_tracker() -> None:
    """The lookup that printed "undefined"."""
    block = TRACKER.split("const CLASSIFICATION_LABEL", 1)[1].split("};", 1)[0]
    labelled = set(re.findall(r"^\s*([a-z_]+):\s*\"", block, flags=re.MULTILINE))

    assert labelled == ENUMS["Classification"]
