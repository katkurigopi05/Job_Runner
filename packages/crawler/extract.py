"""Turn a career page into postings.

Greenhouse publishes a JSON board API, which is both kinder to the site and
far more stable than scraping rendered HTML — one request returns the whole
board instead of one per posting. Adapters that have no such API fall back to
HTML parsing.

Extraction never invents. A field the source did not provide comes back None
rather than guessed, for the same reason the résumé parser works that way:
downstream matching and tailoring treat these as facts.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any, Protocol

import structlog
from pydantic import BaseModel, Field

log = structlog.get_logger(__name__)

_WS_RE = re.compile(r"[ \t]+")


class ExtractedPosting(BaseModel):
    """One posting, as the board reported it."""

    external_id: str
    url: str
    title: str | None = None
    location: str | None = None
    description_raw: str | None = None
    ats_type: str | None = None
    #: Hash of the fields that matter, for change detection per posting.
    content_hash: str = ""


class PostingExtractor(Protocol):
    ats: str

    def board_url(self, company_slug: str) -> str: ...

    def parse(self, body: str, company_slug: str) -> list[ExtractedPosting]: ...


class _TextExtractor(HTMLParser):
    """Collect the visible text of an HTML fragment.

    The stdlib tokenizer rather than tag regexes, because a regex cannot know
    it is inside <script>: tracking code and stylesheet rules were landing in
    description_raw as if a human had written them. That text feeds embeddings
    and the fabrication guard, so junk in it is not cosmetic.
    """

    #: Elements whose contents are code, not prose.
    SKIP = frozenset({"script", "style", "noscript", "template", "svg"})

    #: Elements that end a line. Opening tags break too — a description that
    #: omits its closing </p> should not weld two paragraphs into one word.
    BLOCK = frozenset(
        {
            "p", "div", "br", "li", "ul", "ol", "tr", "table", "section",
            "article", "header", "footer", "blockquote", "pre", "hr",
            "h1", "h2", "h3", "h4", "h5", "h6",
        }
    )  # fmt: skip

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skipping += 1
        elif tag in self.BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skipping = max(0, self._skipping - 1)
        elif tag in self.BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self._parts.append(data)

    @property
    def text(self) -> str:
        return "".join(self._parts)


def strip_html(raw: str | None) -> str | None:
    """Plain text from an HTML fragment, whitespace normalized."""
    if not raw:
        return None

    parser = _TextExtractor()
    parser.feed(raw)
    parser.close()

    text = _WS_RE.sub(" ", parser.text)
    lines = [line.strip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned or None


def posting_hash(posting: ExtractedPosting) -> str:
    from packages.crawler.fetch import content_hash

    return content_hash(
        "\x1f".join(
            [
                posting.external_id,
                posting.title or "",
                posting.location or "",
                posting.description_raw or "",
            ]
        )
    )


class GreenhouseExtractor:
    """Reads the public Greenhouse board API.

    `?content=true` returns full descriptions in the same call, so a whole
    board costs one request rather than one per posting.
    """

    ats = "greenhouse"

    BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"

    def board_url(self, company_slug: str) -> str:
        return self.BOARD_URL.format(slug=company_slug)

    def parse(self, body: str, company_slug: str) -> list[ExtractedPosting]:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            log.warning("greenhouse_board_not_json", company=company_slug)
            return []

        jobs: list[dict[str, Any]] = payload.get("jobs") or []
        postings: list[ExtractedPosting] = []

        for job in jobs:
            job_id = job.get("id")
            if job_id is None:
                continue

            location = None
            raw_location = job.get("location")
            if isinstance(raw_location, dict):
                location = raw_location.get("name") or None
            elif isinstance(raw_location, str):
                location = raw_location or None

            posting = ExtractedPosting(
                external_id=str(job_id),
                url=job.get("absolute_url")
                or f"https://boards.greenhouse.io/{company_slug}/jobs/{job_id}",
                title=job.get("title") or None,
                location=location,
                description_raw=strip_html(job.get("content")),
                ats_type=self.ats,
            )
            posting.content_hash = posting_hash(posting)
            postings.append(posting)

        return postings


EXTRACTORS: dict[str, PostingExtractor] = {
    GreenhouseExtractor.ats: GreenhouseExtractor(),
}


def extractor_for(ats: str) -> PostingExtractor | None:
    return EXTRACTORS.get(ats)


class CompanySeed(BaseModel):
    """One entry in the hand-picked company registry."""

    name: str
    slug: str
    ats: str = "greenhouse"
    careers_url: str | None = None
    domain: str | None = None
    #: Per-company override. Never goes below the global floor.
    poll_interval_s: int = 3600
    tags: list[str] = Field(default_factory=list)


def load_seed(path: str | None = None) -> list[CompanySeed]:
    """Read the company registry from YAML."""
    from pathlib import Path

    import yaml

    location = Path(path or Path(__file__).resolve().parents[2] / "seeds" / "companies.yaml")
    if not location.is_file():
        log.warning("company_seed_missing", path=str(location))
        return []

    data = yaml.safe_load(location.read_text()) or {}
    return [CompanySeed.model_validate(entry) for entry in data.get("companies", [])]
