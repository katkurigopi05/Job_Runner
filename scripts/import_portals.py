"""Import a career-ops portal list into our company registry.

`santifer/career-ops` (MIT) maintains `templates/portals.example.yml` — around
120 companies with their careers URLs and, where they have one, the board API
endpoint. That is the same information `seeds/companies.yaml` holds, in a
different shape.

This reads theirs and writes ours. A script rather than a one-time paste
because their list is maintained: re-running picks up what they added without
touching anything the owner wrote by hand.

## What it will and will not import

An entry is imported only when its ATS and slug can be read off a URL we
recognise — Greenhouse, Lever or Ashby, the three we can both crawl and apply
to. A company whose careers page is its own site (`twilio.com/careers`) is
reported and skipped: we have no extractor for a bespoke page, so adding it to
the registry would mean a crawl cycle that fetches and parses nothing every
hour, forever.

Nothing is ever removed or rewritten. Entries the owner curated stay exactly
as written, and a name already present is skipped rather than merged —
deciding that their "OpenAI" and our "OpenAI" are the same row is a judgement
this script has no business making silently.

Usage:
    python -m scripts.import_portals path/to/portals.yml [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from packages.crawler.company_csv import classify_url
from packages.crawler.extract import CompanySeed, default_seed_path, load_seed

#: Board URLs we can read an ATS and a slug out of. Ordered most specific
#: first so a Greenhouse EU board is not mistaken for something else.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "greenhouse",
        re.compile(
            r"^https?://(?:boards-api|job-boards|boards)"
            r"(?:\.eu)?\.greenhouse\.io/(?:v1/boards/)?(?P<slug>[\w.-]+)",
            re.I,
        ),
    ),
    (
        "lever",
        re.compile(r"^https?://(?:jobs|api)\.lever\.co/(?:v0/postings/)?(?P<slug>[\w.-]+)", re.I),
    ),
    (
        "ashby",
        re.compile(
            r"^https?://(?:jobs\.ashbyhq\.com|api\.ashbyhq\.com/posting-api/job-board)"
            r"/(?P<slug>[\w.-]+)",
            re.I,
        ),
    ),
)


@dataclass
class Skipped:
    name: str
    reason: str


def identify(*urls: str | None) -> tuple[str, str] | None:
    """Read `(ats, slug)` off the first URL that is a board we support.

    Two matchers, in order. `_PATTERNS` above covers the *API* endpoint forms
    a portal list carries (`boards-api.greenhouse.io/v1/boards/{slug}`,
    `api.lever.co/v0/postings/{slug}`), which a careers-page matcher has no
    reason to know about. `company_csv.classify_url` covers the human-facing
    board URLs, including Workable and links to a single posting.

    Workable is why this delegates rather than growing a fourth pattern.
    `packages/ats/workable.py` has existed the whole time and this list never
    mentioned it, so every Workable company in a portal list was silently
    filed as "bespoke careers page we cannot read" — a company we can both
    crawl and apply to, skipped for no reason. One matcher is how that stops
    happening again.
    """
    for url in urls:
        if not url:
            continue
        cleaned = url.strip()
        for ats, pattern in _PATTERNS:
            match = pattern.match(cleaned)
            if match:
                return ats, match.group("slug")
        found = classify_url(cleaned)
        if found:
            return found
    return None


def read_portals(path: Path) -> list[dict[str, object]]:
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = document.get("tracked_companies") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise SystemExit(f"{path}: no `tracked_companies` list found")
    return [entry for entry in entries if isinstance(entry, dict)]


def convert(
    entries: list[dict[str, object]], existing: list[CompanySeed]
) -> tuple[list[CompanySeed], list[Skipped]]:
    known_pairs = {(seed.ats, seed.slug.lower()) for seed in existing}
    known_names = {seed.name.strip().lower() for seed in existing}

    additions: list[CompanySeed] = []
    skipped: list[Skipped] = []

    for entry in entries:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue

        # Their `enabled: false` is a considered decision by someone who has
        # been through this list. Honour it rather than second-guessing.
        if entry.get("enabled") is False:
            skipped.append(Skipped(name, "disabled in the source list"))
            continue

        if name.lower() in known_names:
            skipped.append(Skipped(name, "already in the registry"))
            continue

        identified = identify(
            str(entry.get("api") or "") or None, str(entry.get("careers_url") or "") or None
        )
        if identified is None:
            skipped.append(Skipped(name, "careers page is not an ATS we can read"))
            continue

        ats, slug = identified
        if (ats, slug.lower()) in known_pairs:
            skipped.append(Skipped(name, f"{ats}:{slug} already in the registry"))
            continue

        additions.append(CompanySeed(name=name, slug=slug, ats=ats))
        known_pairs.add((ats, slug.lower()))
        known_names.add(name.lower())

    return additions, skipped


def append_to_registry(additions: list[CompanySeed], seed_path: Path) -> None:
    """Append, never rewrite. The owner's own entries are not this script's."""
    document = yaml.safe_load(seed_path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict) or not isinstance(document.get("companies"), list):
        raise SystemExit(f"{seed_path}: expected a mapping with a `companies` list")

    document["companies"].extend(
        {"name": seed.name, "slug": seed.slug, "ats": seed.ats} for seed in additions
    )
    seed_path.write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("portals", type=Path, help="career-ops templates/portals.example.yml")
    parser.add_argument("--seeds", type=Path, default=None, help="target registry file")
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be added, write nothing"
    )
    args = parser.parse_args(argv)

    seed_path = args.seeds or default_seed_path()
    existing = load_seed(str(seed_path))
    additions, skipped = convert(read_portals(args.portals), existing)

    for seed in additions:
        print(f"  + {seed.name} ({seed.ats}:{seed.slug})")

    unreadable = [s for s in skipped if "not an ATS" in s.reason]
    if unreadable:
        print(f"\n{len(unreadable)} skipped — bespoke careers pages we have no extractor for:")
        for item in unreadable:
            print(f"  - {item.name}")

    print(
        f"\n{len(additions)} to add, {len(skipped)} skipped, {len(existing)} already in {seed_path}"
    )

    if args.dry_run:
        print("dry run: nothing written")
        return 0

    if additions:
        append_to_registry(additions, seed_path)
        print("written. Run `make validate-seeds` to check the new slugs resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
