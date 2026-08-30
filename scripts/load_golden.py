"""`make load-golden` — put the crawled fixture postings into the database.

The twelve postings in `tests/fixtures/golden/postings.json` were crawled from
live Greenhouse, Lever and Ashby boards. They exist so the tailorer and the
scorers can be pointed at real job-description prose rather than at fixtures
written beside the code that reads them.

They live in a JSON file, which means every part of the system downstream of
the crawler — `/matches`, scoring, the review screen — has never seen them.
This loads them so that half of the pipeline can be exercised on a machine
where the crawler cannot reach a board: behind an egress proxy, on a plane, or
in CI.

## These are not live postings and must never look like one

Every row is written with a `https://golden.invalid/` URL and an external id
prefixed `golden-`. Three reasons that matters more than it looks:

- **`.invalid` is reserved by RFC 2606** and can never resolve. An apply run
  against one of these fails at the fetch rather than reaching some unrelated
  site that happens to own a made-up domain.
- The feed would otherwise offer the owner a job they cannot apply to, with
  nothing on the card saying so.
- `crawler/liveness.py` and the 404 sweep in `make validate-seeds` both read
  the posting table. A fixture that looks live would be counted as a board
  that went dead.

Re-runnable. Keyed on the external id like the crawler is, so a second run
updates rather than duplicating.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from sqlalchemy import select

from packages.core import db as core_db
from packages.core.models import Company, Posting
from packages.crawler.fetch import content_hash

GOLDEN = Path("tests/fixtures/golden/postings.json")

#: Reserved by RFC 2606 — guaranteed never to resolve. See the module docstring.
FIXTURE_HOST = "golden.invalid"


def _external_id(company: str, title: str) -> str:
    digest = hashlib.sha256(f"{company}\x1f{title}".encode()).hexdigest()[:12]
    return f"golden-{digest}"


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=GOLDEN)
    args = parser.parse_args(argv)

    if not args.path.is_file():
        print(f"missing golden set: {args.path}")
        return 1

    postings = json.loads(args.path.read_text(encoding="utf-8"))["postings"]
    added = updated = 0

    async with core_db.get_sessionmaker()() as session:
        for item in postings:
            name = item["company"]
            company = await session.scalar(select(Company).where(Company.name == name))
            if company is None:
                company = Company(name=name, careers_url=f"https://{FIXTURE_HOST}/{name.lower()}")
                session.add(company)
                await session.flush()

            external_id = _external_id(name, item["title"])
            digest = content_hash(
                "\x1f".join(
                    [external_id, item["title"], item.get("location") or "", item["description"]]
                )
            )

            existing = await session.scalar(
                select(Posting).where(Posting.external_id == external_id)
            )
            if existing is None:
                session.add(
                    Posting(
                        company_id=company.id,
                        ats_type=item.get("ats") or "greenhouse",
                        external_id=external_id,
                        url=f"https://{FIXTURE_HOST}/{external_id}",
                        title=item["title"],
                        location=item.get("location"),
                        description_raw=item["description"],
                        content_hash=digest,
                    )
                )
                added += 1
            elif existing.content_hash != digest:
                existing.title = item["title"]
                existing.location = item.get("location")
                existing.description_raw = item["description"]
                existing.content_hash = digest
                updated += 1

        await session.commit()

    print(f"{added} added, {updated} updated, {len(postings) - added - updated} unchanged")
    print(f"All on {FIXTURE_HOST} — fixtures, not live postings. Run `make rescore` to score them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
