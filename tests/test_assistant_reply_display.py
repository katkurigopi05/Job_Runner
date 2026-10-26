"""How the dock shows a reply: lists and tables laid out, charts drawn, never as HTML.

A reply quotes postings and recruiter mail, which other people wrote, so the
formatting is built from text and React escapes it. A chart comes only from
the API (`apps/api/chat_figures.py`), and draws in a colour that means nothing
else on the dashboard.
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "apps/web/src"
ASSISTANT = (WEB / "components/assistant.tsx").read_text(encoding="utf-8")
REPLY_TEXT = (WEB / "components/reply-text.tsx").read_text(encoding="utf-8")
REPLY_BLOCKS = (WEB / "lib/reply-blocks.ts").read_text(encoding="utf-8")
CHART = (WEB / "components/assistant-chart.tsx").read_text(encoding="utf-8")
GLOBALS = (WEB / "app/globals.css").read_text(encoding="utf-8")


def test_a_reply_is_never_rendered_as_html() -> None:
    for source in (REPLY_TEXT, REPLY_BLOCKS, CHART):
        assert "dangerouslySetInnerHTML" not in source
        assert "innerHTML" not in source


def test_only_the_assistants_text_is_formatted_the_owners_is_shown_as_typed() -> None:
    assert '{turn.role === "assistant" ? <ReplyText text={turn.text} /> : turn.text}' in ASSISTANT


def test_a_chart_is_drawn_only_from_the_reply_the_api_sent() -> None:
    assert "chart: body.chart," in ASSISTANT
    assert (
        '{turn.role === "assistant" && turn.chart ? <AssistantChart chart={turn.chart} /> : null}'
        in (ASSISTANT)
    )


def test_the_chart_colour_is_its_own_token_in_every_theme() -> None:
    assert "bg-data" in CHART
    # Dark in @theme, then the explicit light theme and the system light theme.
    assert len(re.findall(r"--color-data:\s*#[0-9a-f]{6};", GLOBALS)) == 3


def test_a_value_on_too_few_applications_is_said_in_words_not_only_paler() -> None:
    assert "too few" in CHART
    assert "opacity-40" in CHART
