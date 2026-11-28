"""Bit-identity and memoization checks for `embed.py`, `idf.py`, and scoring helpers.

`Posting.description_embedding` stores `lexical-idf@2` vectors and `Match.score`
is their cosine, so speeding up tokenization, bucket hashing, IDF lookup, or
scoring helpers must not change a single token, count, or float bit.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

import pytest

from packages.core.models import Posting
from packages.matching import embed
from packages.matching.embed import EMBEDDING_DIM, LexicalEmbedder, tokenize
from packages.matching.idf import MIN_DOCUMENTS, DocumentFrequencies
from packages.matching.legitimacy import Tier, Weight, assess, sibling_index, specificity
from packages.matching.score import _proper_nouns, keyword_overlap, missing_terms

GOLDEN = Path(__file__).parent / "fixtures" / "golden" / "postings.json"

CRAFTED_TEXTS: tuple[str, ...] = (
    "",
    "   \n\t\r  ",
    ".",
    "...",
    "a.",
    ".a.",
    "..c++...",
    "Compensation: $150,000 - $190,000, scale to 1,200,000,000 events.",
    "Not thousands: 1,0000 and a,000 and 12,34 and 99,999.",
    "Trailing dots: Go. We write Go... and C#. Also C++.",
    "Leading and internal dots: .net, .NET., node.js, node.js., asp.net, Python 3.12.",
    "Stopwords only: a an the and are experience team work. the. years...",
    (
        "Non-ASCII: \u0130stanbul \u0130I\u0131, Gro\u00dfe Stra\u00dfe, "
        "\u039b\u039f\u0393\u039f\u03a3 \u0391\u0392\u03a3, \u212a8\u017f."
    ),
    "Repeated words: python python python kubernetes kubernetes kafka go go go go.",
)

# --------------------------------------------------------------------------
# Frozen reference implementations (verbatim pre-optimization logic)
# --------------------------------------------------------------------------

_ORACLE_TOKEN_RE = re.compile(r"[a-z0-9+#.]+")
_ORACLE_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?:\D|$))")
_ORACLE_STOPWORDS = frozenset(embed._STOPWORDS_TEXT.split())


def oracle_tokenize(text: str) -> list[str]:
    return [
        stripped
        for token in _ORACLE_TOKEN_RE.findall(_ORACLE_THOUSANDS_RE.sub("", text.lower()))
        if (stripped := token.rstrip("."))
        and len(stripped) > 1
        and stripped not in _ORACLE_STOPWORDS
    ]


def oracle_bucket(token: str, dim: int = EMBEDDING_DIM) -> int:
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % dim


def oracle_from_texts(texts: Iterable[str]) -> tuple[int, dict[str, int]]:
    counts: Counter[str] = Counter()
    total = 0
    for text in texts:
        if not text or not text.strip():
            continue
        total += 1
        counts.update(set(oracle_tokenize(text)))
    return total, dict(counts)


def oracle_idf(total: int, counts: Mapping[str, int], term: str) -> float:
    if not total:
        return 1.0
    return math.log((total + 1) / (counts.get(term, 0) + 1)) + 1.0


def oracle_weigh(total: int, counts: Mapping[str, int], term: str, occurrences: int) -> float:
    if occurrences <= 0:
        return 0.0
    return (1.0 + math.log(occurrences)) * oracle_idf(total, counts, term)


def oracle_encode_one(
    text: str,
    dim: int = EMBEDDING_DIM,
    *,
    total: int = 0,
    counts: Mapping[str, int] | None = None,
) -> list[float]:
    usable = counts is not None and total >= MIN_DOCUMENTS
    term_counts = Counter(oracle_tokenize(text))
    if not term_counts:
        return [0.0] * dim

    vector = [0.0] * dim
    for token, count in term_counts.items():
        if usable and counts is not None:
            weight = oracle_weigh(total, counts, token, count)
        else:
            weight = 1.0 + math.log(count)
        vector[oracle_bucket(token, dim)] += weight

    norm = math.sqrt(sum(v * v for v in vector))
    if norm > 0:
        vector = [v / norm for v in vector]
    return vector


def oracle_proper_nouns(text: str) -> set[str]:
    seen: dict[str, int] = {}
    capitalized: dict[str, int] = {}
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9+#.-]*", text):
        bare = raw.rstrip(".")
        if len(bare) < 2:
            continue
        key = bare.lower()
        seen[key] = seen.get(key, 0) + 1
        if bare[0].isupper():
            capitalized[key] = capitalized.get(key, 0) + 1
    return {token for token, total in seen.items() if capitalized.get(token, 0) == total}


def oracle_keyword_overlap(profile_text_value: str, posting: Posting) -> list[str]:
    profile_tokens = set(oracle_tokenize(profile_text_value))
    posting_tokens = oracle_tokenize(f"{posting.title or ''} {posting.description_raw or ''}")
    seen: list[str] = []
    for token in posting_tokens:
        if token in profile_tokens and token not in seen:
            seen.append(token)
    return seen[:20]


def pack_floats(values: Sequence[float]) -> bytes:
    return struct.pack(f">{len(values)}d", *values)


def golden_entries() -> list[dict[str, str]]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))["postings"]


def sample_corpus() -> list[str]:
    entries = golden_entries()
    docs = [
        f"{entry.get('title', '')}\n"
        f"{entry.get('description_raw') or entry.get('description') or ''}"
        for entry in entries
    ]
    # Expand past MIN_DOCUMENTS (50) with varied real + crafted texts so IDF is usable.
    expanded = (docs * 5) + list(CRAFTED_TEXTS)
    assert len([d for d in expanded if d.strip()]) >= MIN_DOCUMENTS
    return expanded


# --------------------------------------------------------------------------
# Bit-identity against the frozen oracle
# --------------------------------------------------------------------------


def test_tokenize_matches_the_frozen_oracle_on_crafted_and_golden_texts() -> None:
    entries = golden_entries()
    texts = [
        *CRAFTED_TEXTS,
        *(e.get("title", "") for e in entries),
        *(e.get("description_raw") or e.get("description") or "" for e in entries),
    ]
    for text in texts:
        assert tokenize(text) == oracle_tokenize(text), text[:80]


def test_tokenize_returns_a_fresh_mutable_list_every_call() -> None:
    first = tokenize("Python and PostgreSQL.")
    first.append("mutated")

    assert tokenize("Python and PostgreSQL.") == ["python", "postgresql"]


def test_document_frequencies_counts_and_weights_are_bit_identical() -> None:
    corpus = sample_corpus()
    expected_total, expected_counts = oracle_from_texts(corpus)

    frequencies = DocumentFrequencies.from_texts(corpus)

    assert frequencies.total == expected_total
    assert dict(frequencies.counts) == expected_counts
    assert frequencies.usable

    probe_terms = [*list(expected_counts)[:200], "unseen_cobol_term", "python", "000", ""]
    for term in probe_terms:
        got_idf = frequencies.idf(term)
        exp_idf = oracle_idf(expected_total, expected_counts, term)
        assert got_idf == exp_idf
        assert pack_floats([got_idf]) == pack_floats([exp_idf]), term

        for occurrences in (0, -2, 1, 2, 3, 10, 42):
            got_w = frequencies.weigh(term, occurrences)
            exp_w = oracle_weigh(expected_total, expected_counts, term, occurrences)
            assert got_w == exp_w
            assert pack_floats([got_w]) == pack_floats([exp_w]), (term, occurrences)


def test_empty_and_small_document_frequencies_are_bit_identical() -> None:
    empty = DocumentFrequencies.from_texts(["", "   "])
    assert empty.total == 0
    assert empty.idf("python") == 1.0
    assert pack_floats([empty.weigh("python", 3)]) == pack_floats(
        [oracle_weigh(0, {}, "python", 3)]
    )


@pytest.mark.parametrize("dim", [EMBEDDING_DIM, 64])
def test_lexical_embedder_vectors_are_bit_identical_with_and_without_idf(dim: int) -> None:
    corpus = sample_corpus()
    expected_total, expected_counts = oracle_from_texts(corpus)
    frequencies = DocumentFrequencies.from_texts(corpus)

    plain = LexicalEmbedder(dim=dim)
    weighted = LexicalEmbedder(dim=dim, frequencies=frequencies)

    entries = golden_entries()
    inputs = [
        *CRAFTED_TEXTS,
        *(
            f"{e.get('title', '')}\n{e.get('description_raw') or e.get('description') or ''}"
            for e in entries
        ),
    ]

    for text in inputs:
        got_plain = plain.encode_one(text)
        exp_plain = oracle_encode_one(text, dim=dim)
        assert got_plain == exp_plain
        assert pack_floats(got_plain) == pack_floats(exp_plain)

        got_weighted = weighted.encode_one(text)
        exp_weighted = oracle_encode_one(
            text, dim=dim, total=expected_total, counts=expected_counts
        )
        assert got_weighted == exp_weighted
        assert pack_floats(got_weighted) == pack_floats(exp_weighted)


def test_scoring_helpers_preserve_exact_outputs_and_fresh_collections() -> None:
    corpus = sample_corpus()
    frequencies = DocumentFrequencies.from_texts(corpus)
    profile = "Senior Backend Engineer. Python, PostgreSQL, Docker, AWS, Go, C++, node.js, .net."
    company_a = uuid.uuid4()
    company_b = uuid.uuid4()

    postings = [
        Posting(
            id=uuid.uuid4(),
            company_id=company_a if idx % 2 == 0 else company_b,
            url=f"https://example.com/jobs/{idx}",
            title=entry.get("title", ""),
            location=entry.get("location", ""),
            description_raw=entry.get("description_raw") or entry.get("description") or "",
        )
        for idx, entry in enumerate(golden_entries())
    ]
    # Add twin postings at company_a to exercise reposting detection via pre-indexed siblings.
    twin_1 = Posting(
        id=uuid.uuid4(),
        company_id=company_a,
        url="https://example.com/jobs/twin-1",
        title=postings[0].title,
        description_raw=postings[0].description_raw,
    )
    twin_2 = Posting(
        id=uuid.uuid4(),
        company_id=company_a,
        url="https://example.com/jobs/twin-2",
        title=f"  {(postings[0].title or '').upper()}  ",
        description_raw=postings[0].description_raw,
    )
    all_postings = [*postings, twin_1, twin_2]

    for item in all_postings:
        body = item.description_raw or ""
        nouns = _proper_nouns(body)
        assert nouns == oracle_proper_nouns(body)
        nouns.add("__mutated__")
        assert "__mutated__" not in _proper_nouns(body)

        assert keyword_overlap(profile, item) == oracle_keyword_overlap(profile, item)
        # Repeated calls hit caches and must keep returning the same gaps and specificity.
        gaps_first = missing_terms(profile, item, frequencies=frequencies)
        gaps_second = missing_terms(profile, item, frequencies=frequencies)
        assert gaps_first == gaps_second
        spec_first = specificity(body, frequencies)
        spec_second = specificity(body, frequencies)
        assert pack_floats([spec_first]) == pack_floats([spec_second])

    indexed = assess(postings[0], siblings=all_postings)
    pre_indexed = assess(postings[0], sibling_ids=sibling_index(all_postings))
    assert indexed.as_dict() == pre_indexed.as_dict()
    reposting = next(s for s in indexed.signals if s.name == "reposting")
    assert reposting.weight is Weight.CONCERNING
    assert "3 postings" in reposting.finding
    assert indexed.tier in (Tier.CAUTION, Tier.SUSPICIOUS, Tier.HIGH_CONFIDENCE)


# --------------------------------------------------------------------------
# Memoization guarantees (RED before Step 3, GREEN after)
# --------------------------------------------------------------------------


def test_bucket_hashes_each_token_once_per_dimension(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    real_sha256 = hashlib.sha256

    def counting_sha256(data: bytes = b""):
        nonlocal calls
        calls += 1
        return real_sha256(data)

    if hasattr(embed, "_bucket_for"):
        embed._bucket_for.cache_clear()
    monkeypatch.setattr(hashlib, "sha256", counting_sha256)

    embedder = LexicalEmbedder()
    for _ in range(20):
        embedder.encode_one("python postgresql kubernetes python postgresql")

    assert calls == 3


def test_document_frequencies_computes_log_once_per_distinct_term(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frequencies = DocumentFrequencies.from_texts(sample_corpus())
    calls = 0
    real_log = math.log

    def counting_log(x: float) -> float:
        nonlocal calls
        calls += 1
        return real_log(x)

    monkeypatch.setattr(math, "log", counting_log)

    for _ in range(50):
        for term in ("python", "datadog", "palantir", "unseen_xyz"):
            frequencies.weigh(term, 1)

    # At most the table build (or 3 seen terms + 0/1 unseen), never 50 * 4 = 200 calls.
    assert calls <= len(frequencies.counts) + 1
    before = calls
    for _ in range(50):
        for term in ("python", "datadog", "palantir", "unseen_xyz"):
            frequencies.weigh(term, 1)
    assert calls == before
