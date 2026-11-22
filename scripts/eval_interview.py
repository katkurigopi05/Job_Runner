"""Evaluate saved interview citations and run a company-grouped ranker in shadow."""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from statistics import mean
from uuid import UUID, uuid5

import numpy as np
from sqlalchemy import func, select

from packages.core.db import get_sessionmaker
from packages.core.models_interview import InterviewJudgment, InterviewPlan
from packages.interview.plan import CANDIDATE_KINDS, key
from packages.matching.judged_scores import score_ranking
from packages.matching.skill_vocab import find_skills

MIN_LABELS = 30
MIN_COMPANIES = 5


@dataclass(frozen=True)
class Label:
    company: str
    query: str
    source: UUID
    features: tuple[float, ...]
    baseline: float
    grade: int


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z0-9+#.-]{2,}", value.casefold()))


def _label(judgment, plan: InterviewPlan) -> Label | None:
    company = (plan.inputs.get("company") or "").strip().casefold()
    if plan.inputs.get("fixture") or not company:
        return None
    item = next((s for s in plan.progress["evidence"] if s["id"] == judgment.evidence), None)
    coverage = next(
        (
            row
            for row in plan.progress["plan"]["coverage"]
            if row["requirement"]["id"] == judgment.requirement
        ),
        None,
    )
    if not item or not coverage or item["kind"] not in CANDIDATE_KINDS:
        return None
    req = coverage["requirement"]
    required = set(req["skills"])
    skills = {skill.key for skill in find_skills(item["excerpt"])}
    terms = _tokens(req["wording"])
    token_overlap = len(terms & _tokens(item["excerpt"])) / max(1, len(terms))
    semantic_overlap = len(required & skills) / max(1, len(required))
    ranked = coverage["evidence_ids"]
    baseline = 1 / (ranked.index(item["id"]) + 1) if item["id"] in ranked else 0.0
    q = key(company, " ".join(req["wording"].casefold().split()))
    source = uuid5(UUID(int=0), key(item["kind"], " ".join(item["excerpt"].split())))
    return Label(
        company,
        q,
        source,
        (
            1.0,
            baseline,
            token_overlap,
            semantic_overlap,
            float(item["kind"] == "project"),
            float(item["kind"] == "coursework"),
        ),
        baseline,
        judgment.grade,
    )


def shadow(labels: list[Label]) -> dict:
    distinct = {(row.company, row.query, row.source): row for row in labels}
    labels = list(distinct.values())
    companies = sorted({row.company for row in labels}, key=lambda value: key("holdout", value))
    result = {"independent_live_labels": len(labels), "labeled_companies": len(companies)}
    if len(labels) < MIN_LABELS or len(companies) < MIN_COMPANIES:
        return {
            **result,
            "status": "insufficient_labels",
            "minimum_labels": MIN_LABELS,
            "minimum_companies": MIN_COMPANIES,
        }
    held_out = set(companies[: max(1, len(companies) // 4)])
    train = [row for row in labels if row.company not in held_out]
    test = [row for row in labels if row.company in held_out]
    if len({row.grade for row in train}) < 2:
        return {**result, "status": "insufficient_training_grade_variation"}
    x = np.asarray([row.features for row in train])
    y = np.asarray([row.grade for row in train])
    weights = np.linalg.solve(x.T @ x + np.eye(x.shape[1]), x.T @ y)
    groups = defaultdict(list)
    for label in test:
        groups[label.query].append(label)
    baseline, trained = [], []
    for pool in groups.values():
        if len(pool) < 2 or len({row.grade for row in pool}) < 2:
            continue
        grades = {row.source: row.grade for row in pool}
        for rows, metrics in (
            (sorted(pool, key=lambda row: (-row.baseline, row.source)), baseline),
            (sorted(pool, key=lambda row: (-float(weights @ row.features), row.source)), trained),
        ):
            value = score_ranking([row.source for row in rows], grades, 10)["ndcg_at_depth"]
            if value is not None:
                metrics.append(value)
    if not baseline:
        return {**result, "status": "insufficient_held_out_query_variation"}
    return {
        **result,
        "status": "completed_shadow_evaluation",
        "train_labels": len(train),
        "holdout_labels": len(test),
        "holdout_companies": len(held_out),
        "graded_holdout_queries": len(baseline),
        "baseline_ndcg_at_10": mean(baseline),
        "trained_ndcg_at_10": mean(trained),
        "paired_mean_difference": mean(b - a for a, b in zip(baseline, trained, strict=True)),
        "scope": "Judged evidence pool only. Serving ranker is unchanged.",
    }


def citation_audit(plans) -> dict:
    report = {
        "plans": 0,
        "topic_references": 0,
        "coverage_references": 0,
        "unresolved_references": 0,
        "judged_coverage_references": 0,
        "relevant_coverage_references": 0,
    }
    for plan, votes in plans:
        report["plans"] += 1
        material = plan.progress["plan"]
        sources = {item["id"]: item for item in plan.progress["evidence"]}
        for topic in material["topics"]:
            for source in topic["evidence_ids"]:
                report["topic_references"] += 1
                report["unresolved_references"] += int(source not in sources)
        for coverage in material["coverage"]:
            for source in coverage["evidence_ids"]:
                report["coverage_references"] += 1
                report["unresolved_references"] += int(source not in sources)
                grade = votes.get((coverage["requirement"]["id"], source))
                if grade is not None:
                    report["judged_coverage_references"] += 1
                    report["relevant_coverage_references"] += int(grade >= 1)
    return report


async def main() -> None:
    async with get_sessionmaker()() as session:
        count = await session.scalar(select(func.count()).select_from(InterviewJudgment))
        grades = defaultdict(dict)
        labels = []
        rows = await session.execute(
            select(InterviewJudgment, InterviewPlan)
            .join(InterviewPlan)
            .order_by(InterviewPlan.created_at, InterviewJudgment.id)
        )
        for judgment, plan in rows:
            grades[plan.id][judgment.requirement, judgment.evidence] = judgment.grade
            label = _label(judgment, plan)
            if label:
                labels.append(label)
        plans = list(await session.scalars(select(InterviewPlan)))
    print(
        json.dumps(
            {
                "stored_labels_including_fixtures": count,
                "fixture_citations": citation_audit(
                    (plan, grades[plan.id]) for plan in plans if plan.inputs.get("fixture")
                ),
                "live_citations": citation_audit(
                    (plan, grades[plan.id]) for plan in plans if not plan.inputs.get("fixture")
                ),
                "ranker": shadow(labels),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
