"""Write a dated report of how each model tailored one posting.

Generated rather than hand-written, because the numbers are the point and a
table typed out by hand goes stale the moment anything is re-run. Every figure
here is read back out of the database — the résumé rows the tailorer published
and the comparison record on the application — so the report cannot claim a
score no document actually has.

    python -m scripts.model_report [--out storage/reports/model-comparison.md]

**It writes under `storage/` and that is not a detail.** The report quotes the
résumé back — degree titles fused to their dates, whole bullets — because a
parse finding that does not show the line it is about is not actionable. §2.8
makes résumés local-only and `.gitignore` excludes `storage/` for exactly that
reason, so the default output lands somewhere git will not take it. Pointing
`--out` at `docs/` would put the owner's education history in the repository
history, where deleting the file does not remove it.

Two measures, kept apart on purpose. `packages/tailor/ats.py` asks whether a
machine can read the document and whether it carries the posting's vocabulary;
the guard counts say how hard each model pushed against §2.1. Neither is
"which résumé is better", and averaging them would invent a verdict — see
`docs/REFERENCE.md` §3.6 on optimising the one referee we control.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from packages.core.db import get_sessionmaker
from packages.core.models import Application, Posting, Resume
from packages.tailor.ats import score
from packages.tailor.parse import ParsedResume


def _fmt(value: float) -> str:
    return f"{value:.1%}"


async def _gather() -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        app = (
            await session.scalars(
                select(Application).where(Application.status == "needs_review").limit(1)
            )
        ).first()
        if app is None:
            raise SystemExit("no parked application to report on")
        posting = await session.get(Posting, app.posting_id)
        resumes = list(
            (
                await session.scalars(
                    select(Resume)
                    .where(Resume.candidate_id == app.candidate_id)
                    .order_by(Resume.version)
                )
            ).all()
        )
        review = app.review_json or {}
        return {
            "application_id": str(app.id),
            "posting_title": getattr(posting, "title", None),
            "posting_url": getattr(posting, "url", None),
            "job_description": getattr(posting, "description_raw", "") or "",
            "resumes": [
                {
                    "version": r.version,
                    "tailored_by": r.tailored_by,
                    "is_default": r.is_default,
                    "parsed_json": r.parsed_json,
                }
                for r in resumes
            ],
            "comparison": review.get("tailoring_comparison") or [],
        }


def _render(data: dict[str, Any]) -> str:
    jd = data["job_description"]
    now = datetime.now(UTC).astimezone()

    rows: list[tuple[str, Any]] = []
    for r in data["resumes"]:
        if not r["parsed_json"]:
            continue
        parsed = ParsedResume.model_validate(r["parsed_json"])
        who = r["tailored_by"] or ("base (untailored)" if r["is_default"] else "untailored")
        rows.append((f"v{r['version']} {who}", score(parsed, jd)))

    out: list[str] = []
    out.append("# Which model writes the better résumé")
    out.append("")
    out.append(f"<!-- generated {now:%Y-%m-%d %H:%M:%S %Z} by scripts/model_report.py -->")
    out.append(f"<!-- application {data['application_id']} -->")
    out.append("")
    out.append(f"**Posting:** {data['posting_title'] or '—'}  ")
    out.append(f"**URL:** {data['posting_url'] or '—'}  ")
    out.append(f"**Generated:** {now:%Y-%m-%d %H:%M:%S %Z}")
    out.append("")
    out.append(
        "Every number below is read back out of the database rather than typed in, "
        "so the report cannot claim a score no document actually has."
    )
    out.append("")

    # --- ATS ---------------------------------------------------------------
    out.append("## How an ATS reads each version")
    out.append("")
    out.append(
        "`parse` is whether a machine can read the document; `keywords` is the share of "
        "the posting's salient terms the résumé backs. They are kept apart because they "
        "answer different questions, and `overall` weights parse higher only because it "
        "gates — an ATS that cannot find the Experience section never matches inside it."
    )
    out.append("")
    out.append("| résumé | parse | keywords | overall | findings |")
    out.append("|---|---:|---:|---:|---:|")
    for label, rep in rows:
        out.append(
            f"| {label} | {_fmt(rep.parse)} | {_fmt(rep.keywords)} | "
            f"{rep.overall:.3f} | {len(rep.findings)} |"
        )
    out.append("")

    if rows:
        base = next((rep for label, rep in rows if "base" in label), rows[0][1])
        out.append(
            f"Keyword coverage is bounded by what the résumé already supports: "
            f"**{len(base.supported)} of the posting's terms are backed, "
            f"{len(base.missing)} are not.**"
        )
        out.append("")
        out.append("Terms the posting wants and the résumé does not back — the ones that would")
        out.append("move the keyword score, and precisely the ones §2.1 forbids inventing:")
        out.append("")
        out.append("> " + ", ".join(f"`{t}`" for t in base.missing[:25]))
        out.append("")

    # --- findings ----------------------------------------------------------
    out.append("## What each version costs on parse")
    out.append("")
    for label, rep in rows:
        out.append(f"**{label}** — {rep.summary()}")
        out.append("")
        if not rep.findings:
            out.append("- nothing observable stops a parser")
        for f in rep.findings:
            out.append(f"- {f}")
        out.append("")

    # --- guard ------------------------------------------------------------
    out.append("## What each model tried to write")
    out.append("")
    if not data["comparison"]:
        out.append("_No comparison on record for this application yet._")
    else:
        out.append(
            "`guard refused` is a statement about the model — what it tried to write and "
            "the fabrication guard rejected. `provider failed` is a statement about the "
            "network. They are counted apart because adding them together makes a "
            "provider that was down look like one that kept inventing."
        )
        out.append("")
        out.append(
            "| model | answered by | rewritten | unchanged | guard refused | "
            "provider failed | reused |"
        )
        out.append("|---|---|---:|---:|---:|---:|---|")
        for c in data["comparison"]:
            if c.get("error"):
                out.append(
                    f"| {c.get('requested')} | — | — | — | — | — | "
                    f"could not run: {str(c['error'])[:80]} |"
                )
                continue
            out.append(
                f"| {c.get('requested')} | {c.get('answered_by') or '—'} | "
                f"{c.get('changed', 0)} | {c.get('unchanged', 0)} | "
                f"{c.get('rejected', 0)} | {c.get('provider_failures', 0)} | "
                f"{'cache' if c.get('reused') else 'fresh'} |"
            )
    out.append("")

    out.append("## Reading this honestly")
    out.append("")
    out.append(
        "A high refusal count measures how hard a model pushed against the guard, not "
        "whether the résumé got worse — and a low one does not mean the writing improved. "
        "Tuning either number against the guard's own pass rate is the trap "
        "`docs/REFERENCE.md` §3.6 names: it optimises the one referee we control, and a "
        "rewrite can satisfy the guard while reading worse to a person."
    )
    out.append("")
    out.append(
        "The ATS score is bounded in a way worth stating outright: tailoring cannot raise "
        "`keywords`, because the terms that would raise it are the ones the résumé does not "
        "support and the guard exists to refuse. What tailoring can do is re-emphasise what "
        "is already there — and what it can accidentally do is lengthen a bullet past the "
        "point a reader stops, which shows up as a parse finding."
    )
    out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        default="storage/reports/model-comparison.md",
        help="where to write it; keep it under storage/, which is gitignored — "
        "the report quotes résumé lines and §2.8 keeps those off the machine's "
        "git history",
    )
    args = ap.parse_args()

    data = asyncio.run(_gather())
    text = _render(data)
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
