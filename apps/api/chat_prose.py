"""Optional Kaggle prose: selected sources only, with a conservative local guard.

Each sentence must copy source wording. This deliberately rejects novel paraphrases:
without another model, lexical entity heuristics cannot prove a new company/title
was not composed from otherwise allowed words. Reordering sourced sentences is safe.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from apps.api.chat_budget import BudgetExceeded, PromptBudget, pack
from apps.api.chat_selection import EvidenceItem
from packages.llm.prompts import CHAT_PROSE_SYSTEM
from packages.llm.provider import LLMError, LLMProvider
from packages.matching.retrieve import Retrieval

NOTE = "Prose could not be verified; showing the source-based answer."
ANSWER_TOKENS = 600
_CITATION = re.compile(r"\[P([1-9][0-9]*)\]")
_WORD = re.compile(r"[^\W_]+(?:[.'’%-][^\W_]+)*", re.UNICODE)
_NUMBER = re.compile(
    r"(?<!\w)[+-]?\d+(?:[.,]\d+)*(?:%|\s*(?:million|billion|thousand))?|\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion)\b",
    re.I,
)


def _words(text: str) -> str:
    return " ".join(_WORD.findall(text.casefold()))


def computed_context(found: Retrieval) -> str:
    """Numeric context without unselected company names or canonical role titles."""
    lines = [f"Searched {found.searched} open postings."]
    for label, value in (
        ("Complete matching count", found.match_total),
        ("Matching role listings", found.role_total),
        ("Matching title listings", found.title_total),
        ("Ranked listings", found.ranked),
    ):
        if value is not None:
            lines.append(f"{label}: {value}.")
    if found.share is not None:
        share = found.share
        lines.append(f"Computed share: {share.matching} of {share.open_total} open postings.")
        if share.percentage is not None:
            lines.append(f"Computed percentage: {share.percentage}%.")
    return "\n".join(lines)


def validate_prose(raw: str, evidence: Sequence[EvidenceItem], context: str) -> bool:
    if not raw.strip() or len(raw) > 8192:
        return False
    labels = {item.source_label for item in evidence}
    citations = {f"P{match[1]}" for match in _CITATION.finditer(raw)}
    if not citations or not citations <= labels:
        return False
    # Reject malformed citation forms as well as valid-looking unknown IDs.
    if re.search(r"\[[^\]]*\]", _CITATION.sub("", raw)):
        return False
    plain = _CITATION.sub("", raw)
    source = "\n".join([*(item.text for item in evidence), context])
    known_numbers = {m[0].casefold() for m in _NUMBER.finditer(source)}
    if any(m[0].casefold() not in known_numbers for m in _NUMBER.finditer(plain)):
        return False
    # End punctuation followed by a citation or whitespace terminates a sentence;
    # decimals and names such as Node.js are preserved. Every sentence cites its
    # own selected evidence, which must contain the copied factual wording.
    sentences = re.findall(
        r"[^.!?]+(?:[.!?](?!\s|\[|$)[^.!?]+)*[.!?]?(?:\s*\[P[1-9][0-9]*\])+", raw
    )
    if not 1 <= len(sentences) <= 6:
        return False
    if _words(" ".join(sentences)) != _words(raw):
        return False
    for sentence in sentences:
        cited = {f"P{m[1]}" for m in _CITATION.finditer(sentence)}
        text = " ".join(_CITATION.sub("", sentence).split()).casefold()
        supported = [item.text for item in evidence if item.source_label in cited]
        # Match complete sentences, preserving negation, currency and units.
        # A phrase inside "we do not require Python" cannot become "require Python".
        source_sentences = {
            " ".join(part.split()).casefold()
            for source in [*supported, context]
            for part in re.split(r"(?<=[.!?])\s+", " ".join(source.split()))
            if part.strip()
        }
        if not text or text not in source_sentences:
            return False
    return True


async def generate_prose(
    provider: LLMProvider,
    evidence: Sequence[EvidenceItem],
    context: str,
    *,
    budget: PromptBudget | None = None,
) -> str | None:
    def render(labels: frozenset[str]) -> str:
        return "EVIDENCE_PROSE\n" + json.dumps(
            {
                "computed_context": context,
                "evidence": [
                    {"id": item.source_label, "text": item.text}
                    for item in evidence
                    if item.id in labels
                ],
            },
            ensure_ascii=False,
        )

    try:
        if budget is not None:
            prompt, report = pack(
                system=CHAT_PROSE_SYSTEM.text,
                render=render,
                labels=[item.id for item in evidence],
                budget=budget,
                answer_tokens=ANSWER_TOKENS,
            )
            if report.removed_excerpts:
                return None
        else:
            prompt = render(frozenset(item.id for item in evidence))
        # The same wrapped Kaggle provider audits this call and its helper checks
        # the actual tokenized prompt + answer against the 16,384-token context.
        raw = await provider.complete(
            CHAT_PROSE_SYSTEM.text, prompt, max_tokens=ANSWER_TOKENS, temperature=0.0
        )
    except (LLMError, BudgetExceeded):
        return None
    return raw.strip() if validate_prose(raw, evidence, context) else None
