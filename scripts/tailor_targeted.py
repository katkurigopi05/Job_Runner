"""Sort one posting's terms against the base résumé, and revise only where it helps.

    python -m scripts.tailor_targeted <posting-id>
    python -m scripts.tailor_targeted <posting-id> --run [--provider NAME]

Without `--run` nothing is asked of any model and nothing is written: the
posting's terms are sorted four ways against the profile's base résumé
(`packages/tailor/targeted.py`), with where each one's support is, and the
bullets a revision would be asked for are listed.

With `--run` those bullets, and only those, go to the tailoring provider, one
call each and at most one repair. A revision that gained a term is published
as a new résumé version and its PDF is read back (`packages/tailor/verify.py`):
the terms and facts that should be in its text, and the scores before and
after, document to document and PDF to PDF from the same template.

It never attaches what it publishes. No match and no application is touched,
the version is not offered to the tailoring cache, and a fall in any score is
listed for the owner and decides nothing. The scores are heuristics about a
parser and a reader. None predicts a hiring decision.

With nothing to gain it says so and stops before a provider is built.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.db import get_engine, get_sessionmaker
from packages.core.models import Match, Posting, Profile, Project, Resume
from packages.core.storage import get_storage
from packages.github.select import select_projects
from packages.llm import quota
from packages.llm import router as llm_router
from packages.llm.provider import build_provider
from packages.tailor import targeted, verify
from packages.tailor.assemble import assemble_pdf
from packages.tailor.evidence import matched_job_terms
from packages.tailor.parse import ParsedResume
from packages.tailor.publish import apply_rewrites, assembly_options, publish_tailored
from packages.tailor.rewrite import BulletRewrite, TailorResult


def describe_plan(plan: targeted.Plan) -> list[str]:
    """The sorted terms and what would be asked. No résumé text but the bullets'."""
    lines = [f"{len(plan.terms)} terms the posting asks for:"]
    for status in targeted.Status:
        found = plan.having(status)
        lines.append(f"  {status.value}: {len(found)}")
        for term in found:
            if status is targeted.Status.COVERED or status is targeted.Status.GAP:
                continue
            where = ", ".join(term.sources) or "nowhere"
            action = (
                f"bullet {term.bullet + 1} can be revised"
                if term.bullet is not None
                else "no bullet holds the evidence; nothing is revised"
            )
            lines.append(f"    {term.term}: by {term.via!r} in {where}; {action}")
    gaps = plan.gaps
    if gaps:
        lines.append("  gaps, never written in: " + ", ".join(gaps))
    if not plan.targets:
        lines.append(targeted.NO_GAIN)
        return lines
    lines.append(
        f"{len(plan.targets)} of {len(plan.bullets)} bullets would be sent, "
        f"{plan.calls_needed} model calls at most:"
    )
    for index, targets in plan.targets.items():
        asked = ", ".join(f"{target.term} (evidence: {target.evidence})" for target in targets)
        lines.append(f"  bullet {index + 1}: {asked}")
    return lines


def describe_reading(comparison: verify.Comparison) -> list[str]:
    before, after = comparison.before, comparison.after
    rows = [
        ("ATS parse", f"{before.parse:.0%}", f"{after.parse:.0%}"),
        (
            "terms backed, any spelling",
            f"{before.backed} of {before.backed_of}",
            f"{after.backed} of {after.backed_of}",
        ),
        (
            "terms in the posting's words",
            f"{before.stated} of {before.stated_of}",
            f"{after.stated} of {after.stated_of}",
        ),
        ("recruiter", f"{before.recruiter:.0%}", f"{after.recruiter:.0%}"),
        ("  scan", f"{before.scan:.0%}", f"{after.scan:.0%}"),
        ("  qualification", f"{before.qualification:.0%}", f"{after.qualification:.0%}"),
        ("  credibility", f"{before.credibility:.0%}", f"{after.credibility:.0%}"),
        ("  technical", f"{before.technical:.0%}", f"{after.technical:.0%}"),
    ]
    lines = [f"{comparison.representation} to {comparison.representation}:"]
    lines += [f"  {name:30} {was:>10}  ->  {now}" for name, was, now in rows]
    return lines


def describe_check(check: verify.PdfCheck) -> list[str]:
    lines = [
        f"the PDF: {check.pages} pages, {check.extracted_chars} characters of text"
        + (", GARBLED" if check.garbled else ""),
        f"  parse {check.parse:.0%}; findings: {', '.join(check.parse_findings) or 'none'}",
        f"  layout findings: {', '.join(check.layout_findings) or 'none'}",
    ]
    for label, missing in (
        ("terms added and not in the text", check.missing_terms),
        ("figures not in the text", check.missing_figures),
        ("listed technologies not in the text", check.missing_technologies),
    ):
        lines.append(f"  {label}: {', '.join(missing) or 'none'}")
    return lines


async def _load(
    session: AsyncSession, posting_id: uuid.UUID
) -> tuple[Posting, Profile, Resume, ParsedResume, list[Project]]:
    posting = await session.get(Posting, posting_id)
    if posting is None:
        raise SystemExit(f"no posting {posting_id}")
    match = await session.scalar(select(Match).where(Match.posting_id == posting.id))
    if match is None:
        raise SystemExit("this posting has no match, so no profile to read a résumé from")
    profile = await session.get(Profile, match.profile_id)
    base = await session.get(Resume, profile.base_resume_id) if profile else None
    if profile is None or base is None or not base.parsed_json:
        raise SystemExit("the profile has no parsed base résumé")
    parsed = ParsedResume.model_validate(base.parsed_json)

    text = posting.description_raw or ""
    inventory = list(
        (
            await session.scalars(
                select(Project).where(Project.candidate_id == profile.candidate_id)
            )
        ).all()
    )
    # The projects the batch would print for this posting, chosen the same way.
    projects = [
        project
        for project in select_projects(inventory, text)
        if project.pinned or matched_job_terms(project, text)
    ]
    return posting, profile, base, parsed, projects


async def main(posting_id: uuid.UUID, *, run: bool, provider_name: str | None) -> int:
    try:
        async with get_sessionmaker()() as session:
            posting, profile, base, parsed, projects = await _load(session, posting_id)
            text = posting.description_raw or ""
            plan = targeted.classify(parsed, text, projects=projects)
            print(f"{posting.title} | base résumé v{base.version}")
            print("\n".join(describe_plan(plan)))
            if not plan.targets or not run:
                if plan.targets:
                    print("nothing was asked of a model; pass --run to revise those bullets")
                return 0

            provider = (
                build_provider(provider_name) if provider_name else llm_router.tailor_resume()
            )
            name = getattr(provider, "name", "unknown")
            left = quota.remaining(name)
            if left is not None and left < plan.calls_needed:
                print(
                    f"{name} has {left} calls left today and this needs up to {plan.calls_needed}"
                )
                return 1

            revision = await targeted.revise(provider, parsed, text, projects=projects, plan=plan)
            answered = getattr(provider, "answered_by", None) or name
            print(f"\n{revision.message}; answered by {answered}")
            for outcome in revision.outcomes:
                state = f"added {', '.join(outcome.added)}" if outcome.added else "kept as written"
                print(f"  bullet {outcome.bullet + 1}: {state}, {outcome.attempts} calls")
                for reason in outcome.refusals:
                    print(f"    not used: {reason}")
            if not revision.gained:
                print("no revision was kept, so nothing was published")
                return 0

            published = await publish_tailored(
                session,
                candidate_id=profile.candidate_id,
                parsed=parsed,
                result=revision.result,
                projects=projects,
                posting_text=text,
                # Not offered to the cache: a later tailoring for this posting
                # is not to be served a version nobody has chosen.
                tailored_key=None,
                posting_id=posting.id,
                answered_by=answered,
            )
            if published is None:
                print("the PDF could not be rendered or stored")
                return 1
            await session.commit()

            tailored = apply_rewrites(parsed, revision.result, posting_text=text)
            options = assembly_options(parsed)
            base_pdf = assemble_pdf(
                apply_rewrites(parsed, _unchanged(plan), posting_text=text), projects, options
            )
            new_pdf = get_storage().get(published.storage_ref)
            base_check = verify.check_pdf(base_pdf, parsed)
            check = verify.check_pdf(new_pdf, tailored, terms=revision.gained)
            documents = verify.compare(parsed, tailored, text, representation="document")
            pdfs = verify.compare_pdfs(base_pdf, new_pdf, text)

            print(f"\npublished as résumé v{published.version}, attached to nothing")
            print("\n".join(describe_check(check)))
            print("\n".join(describe_reading(documents)))
            print("\n".join(describe_reading(pdfs)))
            flagged = [
                *(f"document: {fall}" for fall in documents.regressions),
                *(f"pdf: {fall}" for fall in pdfs.regressions),
                *verify.new_findings(base_check, check),
                *(() if check.ok else ("the PDF does not hold everything it should; see above",)),
            ]
            print("for review: " + ("; ".join(flagged) if flagged else "nothing fell"))
            print("these scores are heuristics, and nothing was selected or attached")
            return 0
    finally:
        await get_engine().dispose()


def _unchanged(plan: targeted.Plan) -> TailorResult:
    """The base résumé as a result with no rewrite, for rendering it the same way."""
    return TailorResult(bullets=[BulletRewrite(original=b, tailored=b) for b in plan.bullets])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("posting_id", type=uuid.UUID)
    parser.add_argument("--run", action="store_true", help="ask the model for the listed bullets")
    parser.add_argument("--provider", help="a provider by name, instead of the tailoring default")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.posting_id, run=args.run, provider_name=args.provider)))
