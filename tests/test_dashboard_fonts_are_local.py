"""The dashboard's fonts are files in the repo, not a download at build time.

`next/font/google` fetches each font from Google while `next build` runs. The
scheduled CI run of 2026-11-09 failed there ("An error occurred in
`next/font`") with no code change, and a build on a machine without a network
could not succeed at all. The three fonts are SIL OFL 1.1, so their Latin
subsets are kept beside the layout with their licences.
"""

from pathlib import Path

WEB_SOURCE = Path(__file__).resolve().parents[1] / "apps" / "web" / "src"
FONTS = WEB_SOURCE / "app" / "fonts"
FAMILIES = ("InstrumentSans", "JetBrainsMono", "Newsreader")
WOFF2_MAGIC = b"wOF2"
#: The import itself; a comment may still name the module it replaced.
GOOGLE_FONT_IMPORT = 'from "next/font/google"'


def test_no_source_file_loads_a_font_from_google() -> None:
    offenders = [
        str(path.relative_to(WEB_SOURCE))
        for path in WEB_SOURCE.rglob("*.ts*")
        if GOOGLE_FONT_IMPORT in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_each_font_is_in_the_repo_with_its_licence() -> None:
    for family in FAMILIES:
        font = FONTS / f"{family}-latin.woff2"
        assert font.read_bytes()[:4] == WOFF2_MAGIC, font.name
        licence = (FONTS / f"OFL-{family}.txt").read_text(encoding="utf-8")
        assert "SIL Open Font License, Version 1.1" in licence, family


def test_the_layout_names_every_font_file() -> None:
    layout = (WEB_SOURCE / "app" / "layout.tsx").read_text(encoding="utf-8")
    for family in FAMILIES:
        assert f"./fonts/{family}-latin.woff2" in layout, family
