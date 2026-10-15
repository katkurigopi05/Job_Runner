"""A chunk vector says which model made it.

Every chunk was stamped `sentence-transformers`, the backend's name, whatever
model the backend loaded. Retrieval and the chunk pass compare stamps to
decide whether a stored vector is in the same space as the question's, so a
different model of the same 384 dimensions (and several are) would have
been searched as though it were bge-small, and nothing would have refused.

bge-small keeps the bare stamp, which is what the 168,873 stored rows mean,
so nothing has to be re-embedded or migrated. Any other model is stamped with
its own name and lands in a space of its own.
"""

from __future__ import annotations

import inspect

from packages.matching import embed
from packages.matching.embed import DEFAULT_SENTENCE_MODEL, SentenceTransformerEmbedder, stamp_for


def test_bge_small_keeps_the_stamp_the_stored_rows_carry() -> None:
    assert DEFAULT_SENTENCE_MODEL == "BAAI/bge-small-en-v1.5"
    assert stamp_for(DEFAULT_SENTENCE_MODEL) == "sentence-transformers"
    assert SentenceTransformerEmbedder.name == "sentence-transformers"


def test_the_embedder_loads_the_model_its_bare_stamp_means() -> None:
    """Otherwise the bare stamp would mean one model and load another."""
    default = inspect.signature(SentenceTransformerEmbedder).parameters["model_name"].default

    assert default == DEFAULT_SENTENCE_MODEL


def test_another_model_gets_a_space_of_its_own() -> None:
    other = stamp_for("intfloat/e5-small-v2")

    assert other == "sentence-transformers:intfloat/e5-small-v2"
    assert other != stamp_for(DEFAULT_SENTENCE_MODEL)


def test_a_long_model_name_still_fits_the_column_and_stays_unique() -> None:
    long_a = "some-organisation-with-a-long-name/" + "a" * 60
    long_b = "some-organisation-with-a-long-name/" + "a" * 59 + "b"

    assert len(stamp_for(long_a)) <= embed.STAMP_MAX_CHARS
    assert stamp_for(long_a) != stamp_for(long_b)
    assert stamp_for(long_a).startswith("sentence-transformers:some-organisation")
