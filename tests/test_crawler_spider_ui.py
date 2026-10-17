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
