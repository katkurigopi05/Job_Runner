"""Gate 1, live half — run the adapter against a real Greenhouse posting.

    make gate-1-live URL=https://boards.greenhouse.io/<company>/jobs/<id>

This is READ-ONLY. It parses the posting, enumerates the real field list, and
fills from a fixture profile so you can see what the adapter would do. It never
clicks submit.

It exists because the offline suite proves the adapter is internally consistent
against a fixture that was written by hand — it cannot prove the selectors match
Greenhouse's live DOM. Only this does.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

from packages.ats.answers import build_answers
from packages.ats.base import ManualCompletionRequired, SiteError
from packages.ats.registry import adapter_for
from packages.core.models import Candidate, Profile

FIXTURE_CANDIDATE = Candidate(
    id=uuid.uuid4(),
    user_id=uuid.uuid4(),
    name="Ada Lovelace",
    email="ada@example.com",
)

FIXTURE_PROFILE = Profile(
    id=uuid.uuid4(),
    candidate_id=FIXTURE_CANDIDATE.id,
    label="fixture",
    phone="+1-555-0100",
    location="Austin, TX",
    work_auth="US citizen",
    needs_sponsorship=False,
    salary_expectation="$180,000",
    links_json={"linkedin": "https://linkedin.com/in/ada"},
    answers_kv_json={},
)


async def main(url: str) -> int:
    from apps.worker.browser import ephemeral_page

    adapter = adapter_for(url)
    print(f"adapter: {adapter.name}\nurl:     {url}\n")

    # HEADED=1 opens a real window and slows the driver down, so the run can be
    # watched. It changes nothing about what the adapter does.
    headed = os.environ.get("HEADED") == "1"
    async with ephemeral_page(headless=not headed, slow_mo_ms=400 if headed else 0) as page:
        await page.goto(url, wait_until="domcontentloaded")

        try:
            posting = await adapter.parse_posting(page)
        except SiteError as exc:
            print(f"FAIL parse_posting: {exc}")
            return 1

        print("--- parse_posting ---")
        print(f"  title:    {posting.title}")
        print(f"  location: {posting.location}")
        print(f"  id:       {posting.external_id}")
        print(f"  closed:   {posting.closed}")
        print(f"  body:     {len(posting.description_raw or ''):,} chars\n")

        if posting.closed:
            print("posting is closed; nothing further to check")
            return 0

        try:
            questions = await adapter.enumerate_fields(page)
        except ManualCompletionRequired as exc:
            print(f"BLOCKED: {exc}")
            print("This is the expected outcome on a site that blocks automation.")
            return 0
        except SiteError as exc:
            print(f"FAIL enumerate_fields: {exc}")
            return 1

        print(f"--- enumerate_fields: {len(questions)} fields ---")
        for question in questions:
            flag = "*" if question.required else " "
            options = f"  options={[o.label for o in question.options]}" if question.options else ""
            print(f" {flag} [{question.kind.value:<14}] {question.key}")
            print(f"      label: {question.label!r}{options}")

        answers = build_answers(questions, FIXTURE_CANDIDATE, FIXTURE_PROFILE)
        try:
            report = await adapter.fill(page, answers)
        except ManualCompletionRequired as exc:
            # Not a failure. §2.5 — a site that blocks automation is a hard
            # boundary, and refusing is the adapter working. The guard runs
            # again inside fill() because a captcha can mount after the field
            # list is read, which is exactly what Vercel's board does.
            print(f"\nBLOCKED during fill: {exc}")
            print("This is the correct outcome, not a bug. Finish this one by hand.")
            shot = Path("storage/receipts/live-check-blocked.png")
            shot.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(shot), full_page=True)
            print(f"screenshot: {shot}")
            return 0

        print(f"\n--- fill: {len(report.filled)} filled, {report.fill_rate:.0%} of fields ---")
        for field in report.filled:
            print(f"  + {field.key} = {field.value!r}")
        for skipped in report.skipped:
            print(f"  - {skipped.key}: {skipped.reason}")

        print(f"\n--- unanswered: {len(report.unanswered)} ---")
        for question in report.unanswered:
            print(f"  ? {question.question!r}")

        shot = Path("storage/receipts/live-check.png")
        shot.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(shot), full_page=True)
        print(f"\nscreenshot: {shot}")
        print(f"complete:   {report.is_complete}")
        print("\nNothing was submitted.")

    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1])))
