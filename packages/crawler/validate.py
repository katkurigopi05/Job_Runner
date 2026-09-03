"""Validate registry entries without changing the registry.

A Greenhouse API 404 is not proof that a company left Greenhouse: some live
boards are not published through the API. Only classify a slug as missing
after the rendered job board fails too. All requests use PoliteFetcher, so the
60-second per-host floor remains structural and validation stays sequential.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import structlog
import yaml

from packages.crawler.extract import JSONLD_ATS, CompanySeed, extractor_for, load_seed
from packages.crawler.fetch import Blocked, FetchResult, PoliteFetcher, build_fetcher
from packages.crawler.jsonld import job_postings

log = structlog.get_logger(__name__)

RENDERED_BOARD_URL = "https://job-boards.greenhouse.io/{slug}"
_MISSING_MARKERS = (
    "board not found",
    "job board no longer exists",
    "page not found",
)
_BOARD_MARKERS = ("<title", "greenhouse", "open position", "open role", "/jobs/")


class SeedState(StrEnum):
    API = "api"
    RENDERED_ONLY = "rendered_only"
    #: A bespoke careers page still publishing schema.org JobPosting data.
    #: Separate from API because the stamp is read months later and "api" on a
    #: company's own careers page would describe something that never existed.
    STRUCTURED = "structured"
    MISSING = "missing"
    OTHER_ATS = "other_ats"
    #: robots.txt said no, or could not be read. Not a verdict on the slug.
    BLOCKED = "blocked"


@dataclass(frozen=True)
class SeedValidation:
    company: str
    slug: str
    ats: str
    state: SeedState
    api_status: int | None = None
    rendered_status: int | None = None


def _page_publishes_jobs(response: FetchResult) -> bool:
    """A bespoke careers page is alive when it still publishes JobPosting data.

    `_api_is_board` cannot answer this: a careers page returns HTML, so a live
    one would read as MISSING and the sweep would condemn every entry it was
    added to check. What makes a `jsonld` seed dead is the page having stopped
    publishing structured data — the same thing that makes the crawler stop
    finding jobs there — so that is what is measured.
    """
    return bool(response.ok and job_postings(response.text))


def _api_is_board(response: FetchResult) -> bool:
    if not response.ok:
        return False
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and isinstance(payload.get("jobs"), list)


def _rendered_is_board(response: FetchResult) -> bool:
    if not response.ok:
        return False
    body = response.text.lower()
    return not any(marker in body for marker in _MISSING_MARKERS) and any(
        marker in body for marker in _BOARD_MARKERS
    )


async def validate_seeds(seeds: list[CompanySeed], fetcher: PoliteFetcher) -> list[SeedValidation]:
    """Check seeds in order; never delete or rewrite an entry."""
    results: list[SeedValidation] = []

    for seed in seeds:
        extractor = extractor_for(seed.ats)
        if extractor is None:
            # OTHER_ATS means "we cannot check it", not "it is wrong". The
            # entry is reported and left exactly as the owner wrote it.
            results.append(SeedValidation(seed.name, seed.slug, seed.ats, SeedState.OTHER_ATS))
            continue

        try:
            api = await fetcher.fetch(extractor.board_url(seed.slug))
        except Blocked as exc:
            # One unreadable robots.txt must not abandon the rest of the list.
            log.info("seed_validation_blocked", company=seed.name, reason=str(exc))
            results.append(SeedValidation(seed.name, seed.slug, seed.ats, SeedState.BLOCKED))
            continue
        if seed.ats == JSONLD_ATS:
            state = SeedState.STRUCTURED if _page_publishes_jobs(api) else SeedState.MISSING
            results.append(
                SeedValidation(seed.name, seed.slug, seed.ats, state, api_status=api.status)
            )
            continue

        if _api_is_board(api):
            results.append(
                SeedValidation(
                    seed.name,
                    seed.slug,
                    seed.ats,
                    SeedState.API,
                    api_status=api.status,
                )
            )
            continue

        if seed.ats != "greenhouse":
            # Only Greenhouse serves a rendered board at a second URL. For the
            # others a dead API is the whole answer.
            results.append(
                SeedValidation(
                    seed.name, seed.slug, seed.ats, SeedState.MISSING, api_status=api.status
                )
            )
            continue

        rendered = await fetcher.fetch(RENDERED_BOARD_URL.format(slug=seed.slug))
        state = SeedState.RENDERED_ONLY if _rendered_is_board(rendered) else SeedState.MISSING
        results.append(
            SeedValidation(
                seed.name,
                seed.slug,
                seed.ats,
                state,
                api_status=api.status,
                rendered_status=rendered.status,
            )
        )

    return results


def _line(result: SeedValidation) -> str:
    statuses = ""
    if result.api_status is not None:
        statuses += f" api={result.api_status}"
    if result.rendered_status is not None:
        statuses += f" rendered={result.rendered_status}"
    return f"{result.state.value:13} {result.company} ({result.slug}, {result.ats}){statuses}"


def record(
    path: str, results: list[SeedValidation], *, today: str | None = None
) -> tuple[int, int]:
    """Write each verdict back into the registry. Returns (kept, retired).

    Validation used to print and stop, so the verdict lived in a terminal
    scrollback and the file could not answer "which of these has anyone
    checked". That is not a cosmetic gap: the registry grew from 50 to 119 by
    import and the only 404 sweep ran against the original 50, which leaves 90
    entries whose silence is unexplained. A dead board yields zero postings,
    and zero postings is exactly what a live board with nothing new yields.

    A `MISSING` entry moves to a `retired:` block rather than being deleted,
    with the statuses that condemned it. CLAUDE.md has claimed this happened
    since the first sweep; it never did, the entries were simply removed, and
    the argument for keeping them was right all along — a slug that 404s today
    may be a rename rather than a departure, and the evidence is what tells
    those apart later.

    `retired:` is not read by `load_seed`, so a retired board is not polled.
    """
    stamp = today or datetime.now(UTC).date().isoformat()
    verdicts = {(r.slug, r.ats): r for r in results}

    location = Path(path)
    raw = yaml.safe_load(location.read_text()) or {}
    entries = raw.get("companies") or []
    retired = raw.get("retired") or []

    kept: list[dict[str, object]] = []
    for entry in entries:
        verdict = verdicts.get((entry.get("slug"), entry.get("ats", "greenhouse")))
        if verdict is None:
            # Not part of this run — leave whatever it already carried.
            kept.append(entry)
            continue
        entry["checked"] = stamp
        entry["state"] = verdict.state.value
        if verdict.state is SeedState.MISSING:
            entry["api_status"] = verdict.api_status
            entry["rendered_status"] = verdict.rendered_status
            retired.append(entry)
        else:
            kept.append(entry)

    raw["companies"] = kept
    if retired:
        raw["retired"] = retired
    location.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    return len(kept), len(retired)


async def _run(path: str, *, write: bool = False) -> int:
    seeds = load_seed(path)
    never = sum(1 for seed in seeds if seed.checked is None)
    if never:
        print(f"{never} of {len(seeds)} entries have never been validated.")

    results = await validate_seeds(seeds, build_fetcher())
    for result in results:
        print(_line(result))

    missing = [result for result in results if result.state is SeedState.MISSING]
    if write:
        kept, retired = record(path, results)
        print(f"Wrote {path}: {kept} active, {retired} retired.")
    elif missing:
        print("Missing entries require a current slug or ats update; none were deleted.")
        print("Re-run with --write to record every verdict and retire the dead boards.")
    return 1 if missing and not write else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate curated company ATS seeds")
    parser.add_argument("path", nargs="?", default="seeds/companies.yaml")
    parser.add_argument(
        "--write",
        action="store_true",
        help="record each verdict in the registry and move dead boards to `retired:`",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.path, write=args.write)))


if __name__ == "__main__":
    main()
