"""Corpus statistics instead of a hand-written stoplist.

Four modules carry a list of "words that appear in every posting and describe
nothing". Each is a guess at a distribution. This measures it.
"""

from __future__ import annotations

import uuid

from packages.core.models import Posting
from packages.matching.idf import MIN_DOCUMENTS, DocumentFrequencies
from packages.matching.legitimacy import specificity
from packages.matching.score import missing_terms

BOILERPLATE = (
    "We are a fast-paced collaborative team building great products. You will "
    "work with cross-functional stakeholders to drive impact. We offer "
    "competitive compensation and excellent benefits. "
)


def _corpus(size: int = 120) -> list[str]:
    """Postings that all say the same thing, plus one distinguishing tool."""
    tools = ["kubernetes", "spark", "ray", "airflow", "terraform"]
    return [
        f"{BOILERPLATE} Machine learning role using Python. {tools[i % len(tools)]} experience."
        for i in range(size)
    ]


def _posting(text: str, title: str = "Machine Learning Engineer") -> Posting:
    identifier = uuid.uuid4()
    return Posting(
        id=identifier,
        company_id=uuid.uuid4(),
        title=title,
        description_raw=text,
        # NOT NULL, and these fixtures get inserted for the re-embed tests.
        url=f"https://boards.greenhouse.io/acme/jobs/{identifier.hex[:8]}",
        external_id=identifier.hex[:8],
    )


# --------------------------------------------------------------------------
# The statistic
# --------------------------------------------------------------------------


def test_a_term_in_every_document_is_boilerplate() -> None:
    frequencies = DocumentFrequencies.from_texts(_corpus())

    assert frequencies.is_boilerplate("collaborative")
    assert frequencies.is_boilerplate("stakeholders")
    assert not frequencies.is_boilerplate("kubernetes")


def test_a_rarer_term_carries_more_weight() -> None:
    frequencies = DocumentFrequencies.from_texts(_corpus())

    assert frequencies.idf("kubernetes") > frequencies.idf("collaborative")


def test_a_term_repeated_inside_one_document_counts_once() -> None:
    """Document frequency, not term frequency. Otherwise one verbose posting
    could make a term look universal."""
    frequencies = DocumentFrequencies.from_texts(["python python python python", "java"])

    assert frequencies.counts["python"] == 1
    assert frequencies.total == 2


def test_an_unseen_term_gets_the_highest_weight_not_a_crash() -> None:
    frequencies = DocumentFrequencies.from_texts(_corpus())

    assert frequencies.idf("cobol") > frequencies.idf("kubernetes")
    assert frequencies.document_share("cobol") == 0.0


def test_every_weight_stays_positive() -> None:
    """A common term should be weak evidence, never evidence against."""
    frequencies = DocumentFrequencies.from_texts(_corpus())

    assert all(frequencies.idf(term) > 0 for term in frequencies.counts)


def test_a_small_corpus_is_not_trusted() -> None:
    """With 20 postings, a term in three looks rare because the sample is
    small, not because it is."""
    small = DocumentFrequencies.from_texts(_corpus(MIN_DOCUMENTS - 1))
    big = DocumentFrequencies.from_texts(_corpus(MIN_DOCUMENTS))

    assert not small.usable
    assert big.usable


def test_an_empty_corpus_is_inert() -> None:
    frequencies = DocumentFrequencies.from_texts([])

    assert not frequencies.usable
    assert frequencies.idf("anything") == 1.0
    assert frequencies.document_share("anything") == 0.0


def test_blank_documents_are_not_counted() -> None:
    frequencies = DocumentFrequencies.from_texts(["python", "", "   ", "java"])

    assert frequencies.total == 2


# --------------------------------------------------------------------------
# What it changes downstream
# --------------------------------------------------------------------------


def test_a_universal_term_stops_being_reported_as_a_gap() -> None:
    """Someone crawling only ML companies sees "Python" in every posting, so
    it distinguishes nothing. A fixed list cannot know that."""
    corpus = _corpus()
    frequencies = DocumentFrequencies.from_texts(corpus)
    posting = _posting(corpus[0])
    profile = "Backend engineer. Java and Spring."

    measured = missing_terms(profile, posting, frequencies=frequencies)

    assert "kubernetes" in measured
    assert "python" not in measured
    assert "machine" not in measured


def test_the_hand_list_still_covers_a_small_corpus() -> None:
    """Falling back to a considered guess beats trusting a noisy statistic."""
    posting = _posting(_corpus()[0])
    tiny = DocumentFrequencies.from_texts(_corpus(5))

    assert missing_terms("Java developer.", posting, frequencies=tiny) == missing_terms(
        "Java developer.", posting
    )


def test_specificity_uses_the_corpus_when_it_has_one() -> None:
    """The threshold beside this was calibrated on two fixtures written in
    this repo — docs/REFERENCE.md §3.6. A corpus statistic is not circular."""
    corpus = _corpus()
    frequencies = DocumentFrequencies.from_texts(corpus)

    filler = specificity(BOILERPLATE * 3, frequencies)
    real = specificity(corpus[0], frequencies)

    assert real > filler


def test_specificity_without_a_corpus_still_works() -> None:
    assert specificity(BOILERPLATE) > 0.0


# --------------------------------------------------------------------------
# Weighted vectors, and the stamp that keeps them comparable
# --------------------------------------------------------------------------


def test_weighting_changes_the_embedder_name() -> None:
    """The name is stamped onto every vector. A tf vector compared against a
    tf-idf one is noise wearing the shape of a similarity score, so the two
    must not share an identity."""
    from packages.matching.embed import LexicalEmbedder

    frequencies = DocumentFrequencies.from_texts(_corpus())

    assert LexicalEmbedder().name.startswith("lexical@")
    assert LexicalEmbedder(frequencies=frequencies).name.startswith("lexical-idf@")


def test_a_corpus_too_small_to_trust_leaves_the_embedder_unweighted() -> None:
    from packages.matching.embed import LexicalEmbedder

    tiny = DocumentFrequencies.from_texts(_corpus(5))

    assert LexicalEmbedder(frequencies=tiny).name.startswith("lexical@")


def test_idf_weighting_separates_postings_the_boilerplate_hid() -> None:
    """Every posting shares the same filler. Unweighted, that filler is most
    of each vector and they all look alike."""
    from packages.matching.embed import LexicalEmbedder, cosine

    corpus = _corpus()
    frequencies = DocumentFrequencies.from_texts(corpus)

    kubernetes = f"{BOILERPLATE} Machine learning role using Python. kubernetes experience."
    terraform = f"{BOILERPLATE} Machine learning role using Python. terraform experience."

    plain = LexicalEmbedder()
    weighted = LexicalEmbedder(frequencies=frequencies)

    plain_similarity = cosine(*plain.encode([kubernetes, terraform]))
    weighted_similarity = cosine(*weighted.encode([kubernetes, terraform]))

    assert weighted_similarity < plain_similarity


async def test_statistics_hold_still_between_rebuilds(db_session) -> None:
    """A table recomputed every pass would shift continuously and leave every
    posting permanently stale against it."""
    from packages.matching.idf import rebuild_if_stale

    texts = _corpus(100)
    _, first = await rebuild_if_stale(db_session, texts)
    _, second = await rebuild_if_stale(db_session, texts + _corpus(5))

    assert first == 1
    assert second == first, "a 5% corpus increase must not force a rebuild"


async def test_enough_growth_rebuilds(db_session) -> None:
    from packages.matching.idf import rebuild_if_stale

    _, first = await rebuild_if_stale(db_session, _corpus(100))
    _, second = await rebuild_if_stale(db_session, _corpus(200))

    assert second == first + 1


async def test_a_corpus_below_the_floor_never_builds(db_session) -> None:
    from packages.matching.idf import rebuild_if_stale

    frequencies, revision = await rebuild_if_stale(db_session, _corpus(10))

    assert revision is None
    assert not frequencies.usable


async def test_a_vector_from_another_model_is_re_embedded(db_session) -> None:
    """The bug this whole mechanism exists for: switching embedders used to
    leave old vectors in the old space forever, and cosine across spaces
    returns a plausible number rather than an error."""
    from packages.core.models import Company
    from packages.matching.embed import LexicalEmbedder
    from packages.matching.score import embed_postings

    company = Company(name="Acme")
    db_session.add(company)
    await db_session.flush()

    posting = _posting(_corpus()[0])
    posting.company_id = company.id
    db_session.add(posting)
    await db_session.flush()

    first = await embed_postings(db_session, [posting], embedder=LexicalEmbedder())
    assert first == 1
    assert posting.embedding_model.startswith("lexical@")

    # Same postings, different model: every one has to be redone.
    weighted = LexicalEmbedder(frequencies=DocumentFrequencies.from_texts(_corpus()))
    second = await embed_postings(db_session, [posting], embedder=weighted, revision=1)

    assert second == 1
    assert posting.embedding_model.startswith("lexical-idf@")
    assert posting.embedding_revision == 1


async def test_an_unchanged_stamp_does_no_work(db_session) -> None:
    from packages.core.models import Company
    from packages.matching.embed import LexicalEmbedder
    from packages.matching.score import embed_postings

    company = Company(name="Acme")
    db_session.add(company)
    await db_session.flush()
    posting = _posting(_corpus()[0])
    posting.company_id = company.id
    db_session.add(posting)
    await db_session.flush()

    await embed_postings(db_session, [posting], embedder=LexicalEmbedder())

    assert await embed_postings(db_session, [posting], embedder=LexicalEmbedder()) == 0


async def test_a_new_revision_invalidates_the_old_vectors(db_session) -> None:
    """A rebuild changes what the weights mean, so everything weighted by the
    old ones has to be redone. That cost is why rebuilds are on a threshold."""
    from packages.core.models import Company
    from packages.matching.embed import LexicalEmbedder
    from packages.matching.score import embed_postings

    company = Company(name="Acme")
    db_session.add(company)
    await db_session.flush()
    posting = _posting(_corpus()[0])
    posting.company_id = company.id
    db_session.add(posting)
    await db_session.flush()

    weighted = LexicalEmbedder(frequencies=DocumentFrequencies.from_texts(_corpus()))
    await embed_postings(db_session, [posting], embedder=weighted, revision=1)

    assert await embed_postings(db_session, [posting], embedder=weighted, revision=2) == 1
