"""Select existing evidence without accepting model-written factual prose.

The model's entire response must be one JSON object, ``{"ids": [...]}``.
Parsing never repairs text or calls a provider. A failed parse returns no
evidence and a bounded reason, keeping untrusted output out of replies/logs.
Selection validates provenance, not whether an item answers the question;
the caller still owns retrieval, coverage notices and semantic uncertainty.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

MAX_SELECTION = 8
MAX_RESPONSE_CHARS = 16_384

_EvidenceId = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]
_SourceLabel = Annotated[str, StringConstraints(strict=True, pattern=r"^P[1-9][0-9]*$")]

_CUSTOMER_AUDIENCE = r"(?:customers?|clients?)"
_CUSTOMER_SUPPORT_QUERY = re.compile(
    rf"\b(?P<negated>(?:no|not|without)\s+)?(?:"
    rf"{_CUSTOMER_AUDIENCE}(?:\s*/\s*{_CUSTOMER_AUDIENCE})?[\s-]+(?:support|services?)"
    rf"|support(?:\s+(?:for|to))?\s+{_CUSTOMER_AUDIENCE})\b",
    re.IGNORECASE,
)
_CUSTOMER_MENTION = re.compile(rf"\b{_CUSTOMER_AUDIENCE}\b", re.IGNORECASE)


class EvidenceItem(BaseModel):
    """One exact, caller-supplied statement; source labels name posting citations."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: _EvidenceId
    text: str = Field(min_length=1)
    source_label: _SourceLabel | None = None


class EvidenceSelection(BaseModel):
    """The only model-generated fields accepted by the selection protocol."""

    model_config = ConfigDict(extra="forbid", strict=True)

    ids: list[_EvidenceId] = Field(max_length=MAX_SELECTION)


class SelectionFailure(StrEnum):
    """Fixed diagnostics safe to record without model output or source text."""

    INVALID_JSON = "invalid_json"
    INVALID_SHAPE = "invalid_shape"
    UNKNOWN_ID = "unknown_id"
    DUPLICATE_ID = "duplicate_id"
    TOO_MANY_IDS = "too_many_ids"
    RESPONSE_TOO_LARGE = "response_too_large"
    INVALID_CATALOG = "invalid_catalog"


@dataclass(frozen=True)
class SelectionResult:
    """Resolved evidence in model-requested order, or no items on any failure.

    An explicit empty selection has ``failure=None``. The caller can therefore
    distinguish a valid abstention from a malformed response without echoing it.
    """

    items: tuple[EvidenceItem, ...] = ()
    failure: SelectionFailure | None = None

    @property
    def valid(self) -> bool:
        return self.failure is None

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.items)

    @property
    def source_labels(self) -> tuple[str, ...]:
        """Citations for reply metadata, without scanning untrusted quoted text."""
        if not self.valid:
            return ()
        return tuple(dict.fromkeys(item.source_label for item in self.items if item.source_label))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-JSON numeric constant")


def parse_selection(
    raw: str, evidence: Sequence[EvidenceItem], *, max_items: int = MAX_SELECTION
) -> SelectionResult:
    """Resolve a strict JSON selection against an explicit allowlist.

    No partial selection survives a bad ID, duplicate, extra field or trailing
    content. ``max_items`` may narrow the protocol's bound, never raise it;
    an invalid bound is a caller configuration error, rather than model output.
    """
    if type(max_items) is not int or not 1 <= max_items <= MAX_SELECTION:
        raise ValueError(f"max_items must be an integer from 1 to {MAX_SELECTION}")
    catalog = {item.id: item for item in evidence}
    if len(catalog) != len(evidence):
        return SelectionResult(failure=SelectionFailure.INVALID_CATALOG)
    if not isinstance(raw, str):
        return SelectionResult(failure=SelectionFailure.INVALID_JSON)
    if len(raw) > MAX_RESPONSE_CHARS:
        return SelectionResult(failure=SelectionFailure.RESPONSE_TOO_LARGE)
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return SelectionResult(failure=SelectionFailure.INVALID_JSON)
    if (
        isinstance(payload, dict)
        and set(payload) == {"ids"}
        and isinstance(payload["ids"], list)
        and len(payload["ids"]) > max_items
    ):
        return SelectionResult(failure=SelectionFailure.TOO_MANY_IDS)
    try:
        selected = EvidenceSelection.model_validate(payload, strict=True)
    except ValidationError:
        return SelectionResult(failure=SelectionFailure.INVALID_SHAPE)
    if len(set(selected.ids)) != len(selected.ids):
        return SelectionResult(failure=SelectionFailure.DUPLICATE_ID)
    if any(item_id not in catalog for item_id in selected.ids):
        return SelectionResult(failure=SelectionFailure.UNKNOWN_ID)
    return SelectionResult(items=tuple(catalog[item_id] for item_id in selected.ids))


def render_selection(result: SelectionResult) -> str:
    """Copy selected exact texts as clearly marked source material.

    A fence longer than every run in the source prevents embedded instructions,
    Markdown links or citation-looking text from escaping into answer prose.
    Only a validated ``source_label`` supplies a citation outside that fence.
    Invalid and explicitly empty selections render nothing; the caller supplies
    any coverage-preserving fallback rather than presenting an empty answer.
    """
    if not result.valid:
        return ""
    blocks = []
    for item in result.items:
        citation = f" [{item.source_label}]" if item.source_label is not None else ""
        longest = max((len(run) for run in re.findall(r"`+", item.text)), default=0)
        fence = "`" * max(3, longest + 1)
        blocks.append(f"Source text{citation}:\n{fence}text\n{item.text}\n{fence}")
    return "\n\n".join(blocks)


def constrain_selection(result: SelectionResult, question: str) -> SelectionResult:
    """Enforce a necessary audience scope for explicit customer-support queries.

    Customer/client support or service requires an explicit customer/client
    mention in each excerpt. This neither proves full semantic entailment nor
    adds evidence: a generic LLM or employee-support excerpt cannot establish
    the requested audience. Directly negated scope phrases do not activate it.
    Invalid results remain invalid; removing all items is a valid abstention.
    """
    if not result.valid or not result.items:
        return result
    if not any(
        match.group("negated") is None for match in _CUSTOMER_SUPPORT_QUERY.finditer(question)
    ):
        return result
    items = tuple(item for item in result.items if _CUSTOMER_MENTION.search(item.text))
    return result if items == result.items else SelectionResult(items=items)
