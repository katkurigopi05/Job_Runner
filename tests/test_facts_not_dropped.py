"""Bullet-only facts survive rewrites; ordinary rephrasing stays possible."""

from __future__ import annotations

import pytest

from packages.llm.provider import StubProvider
from packages.tailor.guard import SourceCorpus
from packages.tailor.parse import parse_text
from packages.tailor.rewrite import tailor_bullet, vet

MODELS = (
    "Integrated specialized HuggingFace models (Legal-BERT, BERT-NER, DeBERTa-NLI) "
    "for entity extraction and a citation-hallucination verifier, plus a contract-review "
    "pipeline that extracts 25 CUAD clause types with risk levels."
)
RETRIEVAL = (
    "Built a full-stack legal research assistant with a hybrid BM25 + vector retrieval "
    "pipeline (Qdrant) and cross-encoder reranking, orchestrated across CourtListener, "
    "GovInfo, and eCFR data sources."
)
EDITOR = (
    "Implemented packages for editor state, timelines, playback, RAW decoding, "
    "raster effects, export, motion tracking, and background segmentation."
)
PALETTE = (
    "Built and published a color-palette auditing tool (Claude Skill + standalone CLI, "
    "zero dependencies) checking WCAG contrast, hue harmony, Delta-E distinguishability, "
    "and whole-palette redundancy via silhouette-optimal clustering, with a full pytest "
    "suite and GitHub Actions CI."
)


def source(bullet: str) -> SourceCorpus:
    # None of the names inside these bullets is in the stack or Skills list.
    return SourceCorpus.from_resume(
        parse_text(f"Projects\nResearch Tool   [GitHub]\nPython\n{bullet}\nSkills\nPython\n")
    )


@pytest.mark.parametrize(
    "original,candidate,lost",
    [
        (MODELS, MODELS.replace(" (Legal-BERT, BERT-NER, DeBERTa-NLI)", ""), "Legal-BERT"),
        (
            RETRIEVAL,
            "Built a full-stack legal research assistant using a hybrid BM25 and vector "
            "retrieval pipeline with Qdrant, integrating CourtListener, GovInfo, and "
            "eCFR data sources.",
            "cross-encoder",
        ),
        (EDITOR, EDITOR.replace(", and background segmentation", ""), "background segmentation"),
        (EDITOR, EDITOR.replace(", motion tracking", ""), "motion tracking"),
        (
            PALETTE,
            PALETTE.replace(
                "(Claude Skill + standalone CLI, zero dependencies)",
                "using a standalone CLI with no dependencies",
            ),
            "Claude",
        ),
        ("Built services using Whisper for audio.", "Built services for audio.", "Whisper"),
        ("Built services using tinyDB for storage.", "Built services for storage.", "tinyDB"),
        (
            "Built services with the tinydb library for storage.",
            "Built services for storage.",
            "tinydb",
        ),
        (
            "Implemented features (export, motion tracking, segmentation).",
            "Implemented features (motion tracking, segmentation).",
            "export",
        ),
        (
            "Implemented export, motion tracking, and segmentation.",
            "Implemented motion tracking and segmentation.",
            "export",
        ),
    ],
)
def test_a_bullet_only_fact_may_not_disappear(original: str, candidate: str, lost: str) -> None:
    accepted, reason, _ = vet(original, candidate, source(original))
    assert not accepted
    assert reason is not None and reason.startswith("drops ")
    assert lost in reason
    assert "the bullet states it and the rewrite does not" in reason


@pytest.mark.parametrize(
    "original,candidate",
    [
        (
            "Implemented palette generation using k-means and farthest-point sampling, "
            "backed by 32 pytest cases and CI across four Python versions.",
            "Implemented palette generation using k-means and farthest-point sampling, "
            "supported by 32 pytest cases and CI across four Python versions.",
        ),
        ("Shipped a Python tool.", "Developed a Python tool."),
        (
            "Filtered 2,338 incident records into a 1,464-row modeling dataset and swept ten "
            "acreage thresholds with stratified cross-validation, reaching 0.724 held-out "
            "ROC-AUC at the 1,000-acre threshold.",
            "Filtered 2,338 incident records into a 1,464-row modeling dataset and evaluated "
            "ten acreage thresholds with stratified cross-validation, reaching 0.724 held-out "
            "ROC-AUC at the 1,000-acre threshold.",
        ),
        (
            "Built Talend Open Studio ETL jobs to filter, sort, inner-join, and aggregate "
            "delimited product and order-line sources on a shared product key.",
            "Built Talend Open Studio ETL jobs to filter, sort, inner-join, and aggregate "
            "delimited product and order-line sources using a shared product key.",
        ),
        (
            "Added model adapters, audio-upload handling, and graceful fallbacks so the "
            "tool degrades cleanly when a model or API is unavailable.",
            "Added model adapters, audio-upload handling, and graceful fallbacks to ensure "
            "the tool degrades cleanly when a model or API is unavailable.",
        ),
        (MODELS, MODELS.replace("specialized ", "")),
        (
            "Filtered incident records into a modeling dataset.",
            "Reduced incident records into a modeling dataset.",
        ),
        (
            "Built billing queries using PostgreSQL for reports.",
            "Built billing queries using Postgres for reports.",
        ),
        (
            "Implemented export, motion tracking, and segmentation.",
            "Implemented segmentation, export, and motion tracking.",
        ),
    ],
)
def test_wording_changes_keep_the_facts(original: str, candidate: str) -> None:
    accepted, reason, _ = vet(original, candidate, source(original))
    assert accepted, reason


async def test_a_refused_fact_drop_keeps_the_source() -> None:
    candidate = EDITOR.replace(", motion tracking", "")
    result = await tailor_bullet(
        StubProvider(responses={"Original bullet:": candidate}), EDITOR, "", source(EDITOR)
    )
    assert result.guard_refused
    assert not result.changed
    assert result.tailored == EDITOR


def test_a_dropped_fact_is_not_licensed_by_another_bullet() -> None:
    corpus = source(EDITOR + "\nAdded motion tracking for video.")
    accepted, reason, _ = vet(EDITOR, EDITOR.replace(", motion tracking", ""), corpus)
    assert not accepted
    assert reason is not None and "motion tracking" in reason
