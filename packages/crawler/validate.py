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
from enum import StrEnum

from packages.crawler.extract import CompanySeed, GreenhouseExtractor, load_seed
from packages.crawler.fetch import FetchResult, PoliteFetcher, build_fetcher

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
    MISSING = "missing"
    OTHER_ATS = "other_ats"


@dataclass(frozen=True)
class SeedValidation:
    company: str
    slug: str
    ats: str
    state: SeedState
    api_status: int | None = None
    rendered_status: int | None = None


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
    extractor = GreenhouseExtractor()

    for seed in seeds:
        if seed.ats != extractor.ats:
            results.append(SeedValidation(seed.name, seed.slug, seed.ats, SeedState.OTHER_ATS))
            continue

        api = await fetcher.fetch(extractor.board_url(seed.slug))
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


async def _run(path: str) -> int:
    results = await validate_seeds(load_seed(path), build_fetcher())
    for result in results:
        print(_line(result))

    missing = [result for result in results if result.state is SeedState.MISSING]
    if missing:
        print("Missing entries require a current slug or ats update; none were deleted.")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate curated company ATS seeds")
    parser.add_argument("path", nargs="?", default="seeds/companies.yaml")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.path)))


if __name__ == "__main__":
    main()
