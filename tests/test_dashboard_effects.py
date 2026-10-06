"""The dashboard's two effect libraries, and the one meaning each is allowed.

`border-beam` and `thinking-orbs` (MIT, from github.com/Jakubantalik/Libraries.dev)
are the first runtime dependencies the dashboard has taken beyond Next and React.
The owner approved them on 2026-10-05 for two places:

    thinking-orbs   the assistant, while a question is being answered
    border-beam     the application at the head of the review queue

`globals.css` rule 2 is ONE MEANING PER ACCENT, written down after amber drifted
onto eight chips a card. A travelling light is a louder accent than a colour, so
the same drift would cost more: a beam on every card says nothing about any of
them. These tests hold the two placements, and the three things about
`border-beam` that reading its built file turned up:

- its `theme="auto"` reads `prefers-color-scheme` only, so it would ignore the
  dashboard's own light/dark choice (`[data-theme]`, `theme-toggle.tsx`);
- its travelling beam leaves reduced motion to the consumer;
- it carries no `"use client"`, so it can only be imported from a client file.

They read the source rather than a copy of it, like `test_tracker_columns.py`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "apps/web"
SRC = WEB / "src"
PACKAGE = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
SOURCES = {
    path.relative_to(SRC).as_posix(): path.read_text(encoding="utf-8")
    for path in sorted(SRC.rglob("*.ts*"))
}
REVIEW_CARD = SOURCES["app/review/review-card.tsx"]
REVIEW_PAGE = SOURCES["app/review/page.tsx"]
ASSISTANT = SOURCES["components/assistant.tsx"]
DISPLAY = SOURCES.get("lib/display.ts", "")


def importers(package: str) -> list[str]:
    pattern = re.compile(rf"""from\s+["']{re.escape(package)}["']""")
    return [name for name, text in SOURCES.items() if pattern.search(text)]


def test_the_dashboard_depends_on_exactly_these_five() -> None:
    """A third effect library is a decision, not something an install leaves behind."""
    assert set(PACKAGE["dependencies"]) == {
        "next",
        "react",
        "react-dom",
        "border-beam",
        "thinking-orbs",
    }


def test_every_dependency_is_pinned_to_one_version() -> None:
    """As Next and React already are: a range lets a publish change what ships."""
    ranged = {
        name: version
        for name, version in PACKAGE["dependencies"].items()
        if not re.fullmatch(r"\d+\.\d+\.\d+", version)
    }
    assert not ranged, ranged


def test_the_beam_is_imported_by_the_review_card_and_nothing_else() -> None:
    assert importers("border-beam") == ["app/review/review-card.tsx"]


def test_the_orb_is_imported_by_the_assistant_and_nothing_else() -> None:
    assert importers("thinking-orbs") == ["components/assistant.tsx"]


def test_both_importers_are_client_files() -> None:
    """Neither package declares `"use client"`; a server file importing one fails the build."""
    for name in importers("border-beam") + importers("thinking-orbs"):
        assert SOURCES[name].lstrip().startswith('"use client"'), name


def test_only_the_head_of_the_queue_is_lit() -> None:
    """Oldest first, so the first card is the next decision. One beam a screen."""
    assert re.search(r"upNext=\{index === 0\}", REVIEW_PAGE), "the page must light one card"
    assert len(re.findall(r"<BorderBeam\b", REVIEW_CARD)) == 1


def test_the_beam_is_switched_not_mounted() -> None:
    """Every card is wrapped and `active` decides, so the tree keeps its shape.

    Wrapping only the lit card would remount the next one when it moves up the
    queue, and a remount drops the answers the owner had typed into it.
    """
    beam = re.search(r"<BorderBeam\b[^>]*>", REVIEW_CARD, re.S)
    assert beam, "no <BorderBeam> in the review card"
    assert re.search(r"active=\{[^}]*upNext[^}]*\}", beam.group(0)), beam.group(0)


def test_the_beam_stops_for_reduced_motion() -> None:
    beam = re.search(r"<BorderBeam\b[^>]*>", REVIEW_CARD, re.S)
    assert beam and "usePrefersReducedMotion" in REVIEW_CARD
    assert re.search(r"active=\{[^}]*reducedMotion[^}]*\}", beam.group(0)), beam.group(0)
    assert "prefers-reduced-motion" in DISPLAY


def test_the_beam_follows_the_dashboards_theme_not_the_systems() -> None:
    """`theme="auto"` in border-beam never looks at `[data-theme]`."""
    beam = re.search(r"<BorderBeam\b[^>]*>", REVIEW_CARD, re.S)
    assert beam and 'theme="auto"' not in beam.group(0)
    assert re.search(r"theme=\{theme\}", beam.group(0)), beam.group(0)
    assert "useResolvedTheme" in REVIEW_CARD
    assert "data-theme" in DISPLAY and "prefers-color-scheme" in DISPLAY


def test_the_orb_shows_only_while_a_question_is_out() -> None:
    orb = re.search(r"\{busy \? \((.*?)\) : null\}", ASSISTANT, re.S)
    assert orb and "<ThinkingOrb" in orb.group(1), "the orb must sit behind `busy`"
    assert len(re.findall(r"<ThinkingOrb\b", ASSISTANT)) == 1
