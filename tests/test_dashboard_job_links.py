"""Where the dashboard's job links open, and the tab name that replaced it.

Two defects, reproduced on 2026-10-29 by driving Chromium against the
dashboard:

- **A tab keeps `window.name` when it navigates.** The dashboard opened inside
  the tab a posting link had opened was itself `jobrunner-form`, so every later
  posting link, with or without `noreferrer`, loaded into the dashboard, and
  Back reloaded the page from the top. `ReleaseWindowName` in the root layout
  gives the name back on every page load.
- **A job page could redirect the dashboard** through `window.opener`. The
  owner chose the split: postings share one reused tab, which needs the opener
  link, and application forms open a new tab with no way back, since sign-in
  pages are the likeliest to redirect their opener.

`lib/external.ts` is the one place that decides, so its rules are run through
node rather than restated here, as `test_rebinding_guard.py` does.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "apps/web/src"
EXTERNAL = SRC / "lib/external.ts"
SOURCES = {
    path.relative_to(SRC).as_posix(): path.read_text(encoding="utf-8")
    for path in sorted(SRC.rglob("*.ts*"))
}

POSTING_LINKS = [
    "components/swipe-deck.tsx",
    "components/judgment-deck.tsx",
    "components/label-deck.tsx",
    "app/matches/page.tsx",
    "app/review/review-card.tsx",
    "app/postings/[id]/page.tsx",
]
APPLY_LINKS = ["app/applications/[id]/apply/page.tsx"]
APPLY_BUTTONS = ["components/finish-queue.tsx", "components/unsettled-submissions.tsx"]

#: What may only appear in `lib/external.ts`: a JSX target or rel, or a
#: window.open. Anywhere else, a page has chosen for itself again.
_CHOOSES_A_WINDOW = re.compile(r"""\btarget=["{]|\brel=["{]|window\.open\(""")


def _node_strips_types(node: str) -> bool:
    version = subprocess.run([node, "--version"], capture_output=True, text=True, check=False)
    major, minor, *_ = (int(part) for part in version.stdout.strip().lstrip("v").split("."))
    return (major, minor) >= (22, 18)


def _run_external(body: str) -> dict[str, object]:
    """Run `body` with lib/external.ts imported as `ext`; it prints one JSON value."""
    node = shutil.which("node")
    if node is None or not _node_strips_types(node):
        pytest.skip("needs node 22.18+, which runs TypeScript as is; CI's gates job sets it up")
    script = f"import * as ext from {json.dumps(str(EXTERNAL))};{body}"
    run = subprocess.run(
        [node, "--input-type=module", "--no-warnings", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    result: dict[str, object] = json.loads(run.stdout)
    return result


def test_a_dashboard_tab_gives_back_every_name_the_dashboard_gives() -> None:
    names = ["jobrunner-form", "jobrunner-doc", "", "jobrunner", "a-name-of-the-owners"]

    result = _run_external(
        f"console.log(JSON.stringify({{released: {json.dumps(names)}.map(ext.releasedName),"
        " posting: ext.POSTING_WINDOW, doc: ext.DOC_WINDOW, all: ext.WINDOW_NAMES}));"
    )

    assert result["released"] == ["", "", "", "jobrunner", "a-name-of-the-owners"]
    # The values are what tabs already open carry. A renamed one would leave
    # those unrecognised, and the dashboard tab named after the old value.
    assert result["posting"] == "jobrunner-form"
    assert result["doc"] == "jobrunner-doc"
    assert result["all"] == ["jobrunner-form", "jobrunner-doc"]


def test_postings_share_one_tab_and_forms_get_a_new_one_with_no_way_back() -> None:
    result = _run_external(
        "const calls = [];"
        "globalThis.window = { open: (...args) => { calls.push(args); return null; } };"
        "ext.openApplyForm('https://boards.example/acme/apply');"
        "ext.openDocument('/api/resumes/1/download');"
        "console.log(JSON.stringify({posting: ext.POSTING_LINK, apply: ext.APPLY_LINK, calls}));"
    )

    # No `rel` on purpose: a tab opened with noopener is not found by name
    # again, so every click opened a new tab (measured in Chromium).
    assert result["posting"] == {"target": "jobrunner-form"}
    assert result["apply"] == {"target": "_blank", "rel": "noopener noreferrer"}
    assert result["calls"] == [
        ["https://boards.example/acme/apply", "_blank", "noopener,noreferrer"],
        ["/api/resumes/1/download", "jobrunner-doc"],
    ]


def test_every_page_load_runs_the_release() -> None:
    """The root layout mounts on every full page load, the only way a named
    tab turns into the dashboard."""
    layout = SOURCES["app/layout.tsx"]
    release = SOURCES["components/release-window-name.tsx"]

    assert 'from "@/components/release-window-name"' in layout
    assert "<ReleaseWindowName />" in layout
    assert release.startswith('"use client";')
    runs_on_mount = r"useEffect\(\(\) => \{\s*window\.name = releasedName\(window\.name\);"
    assert re.search(runs_on_mount, release)


def test_only_lib_external_decides_where_a_link_opens() -> None:
    offenders = sorted(
        f"{name}: {match.group(0)}"
        for name, text in SOURCES.items()
        if name != "lib/external.ts"
        for match in _CHOOSES_A_WINDOW.finditer(text)
    )

    assert offenders == []


@pytest.mark.parametrize("name", POSTING_LINKS)
def test_a_posting_link_opens_in_the_posting_tab(name: str) -> None:
    assert "{...POSTING_LINK}" in SOURCES[name]


@pytest.mark.parametrize("name", APPLY_LINKS)
def test_an_application_form_link_opens_a_new_tab(name: str) -> None:
    assert "{...APPLY_LINK}" in SOURCES[name]


@pytest.mark.parametrize("name", APPLY_BUTTONS)
def test_an_application_form_button_opens_a_new_tab(name: str) -> None:
    assert "openApplyForm(" in SOURCES[name]
