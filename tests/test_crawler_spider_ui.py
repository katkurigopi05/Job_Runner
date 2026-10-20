"""Controlled fixtures for the browser-facing crawl indicator state."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "apps/web/src/lib/crawler-state.ts"
COMPONENT = ROOT / "apps/web/src/components/crawler-spider.tsx"


def _node_strips_types(node: str) -> bool:
    version = subprocess.run([node, "--version"], capture_output=True, text=True, check=False)
    major, minor, *_ = (int(part) for part in version.stdout.strip().lstrip("v").split("."))
    return (major, minor) >= (22, 18)


def test_running_stalled_idle_and_disconnect_are_distinct() -> None:
    node = shutil.which("node")
    if node is None or not _node_strips_types(node):
        pytest.skip("needs node 22.18+, which executes the TypeScript state module")

    now = 1_800_000_000_000
    fixtures = [
        [None, True],
        [{"running": True, "pending": 1, "stalled": False}, True],
        [{"running": False, "pending": 3, "stalled": True}, True],
        [
            {
                "running": False,
                "pending": 0,
                "stalled": False,
                "newest_posting_at": "2027-01-15T04:00:00Z",
            },
            True,
        ],
        [{"running": True, "pending": 1, "stalled": False}, False],
    ]
    script = (
        f"import {{ crawlerView }} from {json.dumps(str(STATE))};"
        f"const fixtures = {json.dumps(fixtures)};"
        f"console.log(JSON.stringify(fixtures.map(([status, reachable]) => "
        f"crawlerView(status, reachable, {now}))));"
    )
    run = subprocess.run(
        [node, "--input-type=module", "--no-warnings", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    checking, running, stalled, idle, disconnected = json.loads(run.stdout)

    assert (checking["label"], checking["moving"], checking["asleep"]) == (
        "checking crawl",
        False,
        False,
    )
    assert (running["label"], running["moving"], running["asleep"]) == (
        "crawling",
        True,
        False,
    )
    assert (stalled["label"], stalled["moving"], stalled["asleep"]) == (
        "3 queued",
        False,
        False,
    )
    assert idle["label"].startswith("postings ")
    assert (idle["moving"], idle["asleep"]) == (False, True)
    assert (disconnected["label"], disconnected["moving"], disconnected["asleep"]) == (
        "crawl status unavailable",
        False,
        False,
    )


def test_the_component_renders_the_shared_state_instead_of_hiding_failures() -> None:
    source = COMPONENT.read_text(encoding="utf-8")

    assert 'from "@/lib/crawler-state"' in source
    assert "crawlerView(status, reachable)" in source
    assert "if (mounted) setReachable(false)" in source
    assert "if (!reachable" not in source
    assert 'aria-live="polite"' in source


def _run_state(expression: str) -> object:
    node = shutil.which("node")
    if node is None or not _node_strips_types(node):
        pytest.skip("needs node 22.18+, which executes the TypeScript state module")
    script = (
        f"import {{ backlogView, durationInWords }} from {json.dumps(str(STATE))};"
        f"console.log(JSON.stringify({expression}));"
    )
    run = subprocess.run(
        [node, "--input-type=module", "--no-warnings", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(run.stdout)


def _stage(stage: str, kind: str, **values: object) -> dict:
    return {
        "stage": stage,
        "kind": kind,
        "waiting": 0,
        "scheduled": 0,
        "running": 0,
        "items": 0,
        "done_recently": 0,
        "per_minute": 0,
        "eta_minutes": None,
        "after": None,
        **values,
    }


def test_the_backlog_names_what_is_left_and_when() -> None:
    """Asked for on 2026-10-19, so the owner need not ask how long is left."""
    status = {
        "running": True,
        "pending": 1,
        "stalled": False,
        "backlog": [
            _stage("boards", "crawl_company"),
            _stage(
                "discovery",
                "discover_company",
                waiting=3959,
                running=15,
                items=2621,
                per_minute=14,
                eta_minutes=283.9,
            ),
            _stage(
                "deep dives",
                "deep_dive_company",
                waiting=2132,
                items=2132,
                after="discovery",
            ),
        ],
    }

    view = _run_state(f"backlogView({json.dumps(status)})")

    assert view["text"] == "discovery 2,621 left ~4h 44m · deep dives 2,132 left after discovery"
    discovery_line, deep_line = view["title"].split("\n")
    assert discovery_line == (
        "discovery: 3,974 tasks for 2,621 companies, 15 running, "
        "14 a minute over the last 30 minutes, about 4h 44m to go."
    )
    assert deep_line.endswith("not moving yet, starts after discovery.")


def test_nothing_left_shows_nothing() -> None:
    status = {"running": False, "pending": 0, "stalled": False, "backlog": []}

    assert _run_state(f"backlogView({json.dumps(status)})") is None
    assert _run_state("backlogView(null)") is None


def test_durations_read_in_minutes_hours_and_days() -> None:
    assert _run_state("[0.4, 45, 60, 119.6, 1500, 2880].map(durationInWords)") == [
        "<1m",
        "45m",
        "1h",
        "2h",
        "1d 1h",
        "2d",
    ]


def test_the_component_shows_the_backlog_only_from_a_fresh_response() -> None:
    source = COMPONENT.read_text(encoding="utf-8")

    assert "reachable ? backlogView(status) : null" in source
    assert "title={backlog.title}" in source
