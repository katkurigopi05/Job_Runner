"""Download the public board directories discovery reads — `make fetch-board-directories`.

Two files into `storage/board_directories/`, which is gitignored like the rest
of `storage/`, plus an ATTRIBUTION.md. The Figshare directory is CC BY 4.0, so
its attribution travels with the copy; the Job Board Directory asks for none.
See `packages/crawler/board_directory.py` for how they are used.

Fetched through the crawler's own PoliteFetcher, so robots.txt and the per-host
floor apply to these two requests as to any other.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from packages.crawler.board_directory import directory_root, load_default
from packages.crawler.fetch import build_fetcher


@dataclass(frozen=True)
class Source:
    filename: str
    url: str
    attribution: str


SOURCES = (
    Source(
        filename="figshare-ats-company-directory-2026-08.csv",
        url="https://ndownloader.figshare.com/files/67239956",
        attribution=(
            'Mahesh Bandaru, "ATS Company Directory: 9,935 Companies Mapped to '
            'Greenhouse, Lever and Ashby Public Job Boards", Figshare, 2026. '
            "https://figshare.com/articles/dataset/33154145 — CC BY 4.0 "
            "(https://creativecommons.org/licenses/by/4.0/)."
        ),
    ),
    Source(
        filename="job-board-directory.csv",
        url="https://kalebconfer-sys.github.io/job-board-directory/job-board-directory.csv",
        attribution=(
            "Job Board Directory, https://kalebconfer-sys.github.io/job-board-directory/ "
            "— free to download, no attribution required by the publisher."
        ),
    ),
)


async def main() -> None:
    root = directory_root()
    root.mkdir(parents=True, exist_ok=True)
    async with build_fetcher() as fetcher:
        for source in SOURCES:
            response = await fetcher.fetch(source.url)
            if not response.ok:
                raise SystemExit(f"{source.url}: HTTP {response.status}")
            (root / source.filename).write_text(response.text, encoding="utf-8")
            print(f"saved {source.filename} ({len(response.text):,} characters)")
    (root / "ATTRIBUTION.md").write_text(
        "# Board directories\n\n" + "\n".join(f"- {s.attribution}" for s in SOURCES) + "\n",
        encoding="utf-8",
    )
    load_default.cache_clear()
    directory = load_default()
    print(f"{len(directory) if directory else 0:,} boards on the platforms discovery reads")


if __name__ == "__main__":
    asyncio.run(main())
