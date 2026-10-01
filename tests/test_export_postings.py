"""`make export-postings` — the parts a wrong answer would hide in.

A deadline read wrong marks a live job expired, and "delete what expired" is
what the column exists for. A vector that does not round-trip is a CSV whose
similarities quietly disagree with the database's. Neither raises.
"""

from __future__ import annotations

import csv
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pytest

from scripts.export_postings import (
    COLUMNS,
    MODEL,
    encode,
    expiry,
    job_type,
    read_deadline,
    vector_text,
    write,
)

POSTED = date(2026, 9, 1)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Verbatim shapes from the owner's corpus.
        (
            "Application Deadline: December 16, 2026\nPosition Overview",
            (date(2026, 12, 16), "stated in posting"),
        ),
        (
            "Application Deadline: December 09, 2026 Equity",
            (date(2026, 12, 9), "stated in posting"),
        ),
        (
            "Application Deadline:\nThe anticipated application window is 30 days "
            "from the date job is posted",
            (date(2026, 10, 1), "posted date + 30-day window stated in posting"),
        ),
        (
            "Application deadline: applications accepted on an ongoing basis "
            "until position is closed and filled",
            (None, "open until filled"),
        ),
        ("Applications close on 15 October 2026.", (date(2026, 10, 15), "stated in posting")),
        ("Closing date: 2026-11-02", (date(2026, 11, 2), "stated in posting")),
        ("This posting will expire on Sept 5th, 2026", (date(2026, 9, 5), "stated in posting")),
        ("We build payments infrastructure.", (None, "")),
    ],
)
def test_a_deadline_is_read_from_the_postings_own_words(text: str, expected: tuple) -> None:
    assert read_deadline(text, POSTED) == expected


def test_a_numeric_date_is_refused_rather_than_guessed() -> None:
    """10/11/2026 is October in one country and November in another.

    A wrong deadline marks a live job expired, and the column exists to decide
    what gets deleted.
    """
    assert read_deadline("Applications close 10/11/2026.", POSTED) == (
        None,
        "deadline mentioned, date not readable",
    )


def test_a_date_without_a_year_is_the_next_one_after_posting() -> None:
    """Posted in September, "apply by March 3" means next March."""
    assert read_deadline("Please apply by March 3.", POSTED) == (
        date(2027, 3, 3),
        "stated in posting",
    )


def test_a_date_far_from_the_cue_is_not_its_deadline() -> None:
    far = "Application deadline: rolling. " + "x " * 60 + "Founded December 1, 2019."
    assert read_deadline(far, POSTED)[0] is None


@pytest.mark.parametrize(
    ("closed", "deadline", "expected"),
    [
        (date(2026, 9, 10), None, "yes"),
        (date(2026, 9, 10), date(2027, 1, 1), "yes"),
        (None, date(2026, 9, 1), "yes"),
        (None, date(2026, 12, 1), "no"),
        (None, None, "unknown"),
    ],
)
def test_expiry(closed: date | None, deadline: date | None, expected: str) -> None:
    """Removed from the board wins; a stated date decides the rest; silence is unknown."""
    assert expiry(closed, deadline, today=date(2026, 9, 30)) == expected


def test_job_types_come_from_the_curated_title_table() -> None:
    assert job_type("AI Engineer") == "AI / ML Engineer"
    assert (
        job_type("Member of Technical Staff (Software Engineer, Data Platform)")
        == "Software Engineer"
    )
    assert job_type("Account Executive") == "Other"
    assert job_type(None) == "Other"


def test_a_vector_round_trips_float32_exactly() -> None:
    """The CSV and pgvector hold the same numbers, or similarities disagree."""
    vector = np.random.default_rng(0).standard_normal(384).astype(np.float32)
    text = vector_text(vector.tolist())
    back = np.array(text[1:-1].split(","), dtype=np.float32)
    assert text.startswith("[") and text.endswith("]")
    assert np.array_equal(back, vector)


def test_the_csv_has_every_column_and_one_row_per_posting(tmp_path: Path) -> None:
    out = tmp_path / "postings.csv"
    rows = [
        (
            "Acme",
            "AI Engineer",
            "https://x.test/1",
            "Application Deadline: December 16, 2026",
            datetime(2026, 9, 1),
            datetime(2026, 9, 18),
            None,
        ),
        (
            "Acme",
            "Barista",
            "https://x.test/2",
            "Coffee.",
            datetime(2026, 8, 1),
            datetime(2026, 9, 1),
            datetime(2026, 9, 5),
        ),
    ]

    tally = write(out, rows, [[0.5, 0.25], [1.0, 0.0]], today=date(2026, 9, 30))

    with out.open(newline="", encoding="utf-8") as handle:
        written = list(csv.DictReader(handle))
    assert tuple(written[0]) == COLUMNS
    first, second = written
    assert (first["job_type"], first["application_deadline"], first["expired"]) == (
        "AI / ML Engineer",
        "2026-12-16",
        "no",
    )
    assert (second["closed_on"], second["expired"]) == ("2026-09-05", "yes")
    assert first["embedding_model"] == MODEL
    assert first["description_embedding"] == "[0.5,0.25]"
    assert tally == {"expired=no": 1, "expired=yes": 1}


def test_only_new_or_changed_text_is_encoded(tmp_path: Path, monkeypatch) -> None:
    """The cache is keyed by the text, not the posting.

    A posting whose description changed must be re-encoded; keyed on its id,
    the export would carry the old vector beside the new text.
    """
    import packages.matching.embed as embed_module

    seen: list[str] = []

    class Fake:
        def __init__(self, model_name: str) -> None:
            pass

        def encode(self, texts: list[str]) -> list[list[float]]:
            seen.extend(texts)
            return [[float(len(t)), 1.0] for t in texts]

    monkeypatch.setattr(embed_module, "SentenceTransformerEmbedder", Fake)
    cache = tmp_path / "cache.npz"

    first = encode(["alpha", "beta"], cache)
    second = encode(["alpha", "beta changed", "alpha"], cache)

    assert first == [[5.0, 1.0], [4.0, 1.0]]
    assert sorted(seen) == ["alpha", "beta", "beta changed"], "alpha was encoded once"
    assert second == [[5.0, 1.0], [12.0, 1.0], [5.0, 1.0]]


def test_the_script_imports_without_the_embeddings_extra() -> None:
    """CI installs no embeddings extra, so the model may only load when encoding.

    Blocking the packages makes any top-level import of them fail here, in a
    fresh interpreter where nothing has already imported them.
    """
    import subprocess

    blocked = (
        "import sys; sys.modules['sentence_transformers'] = None; sys.modules['torch'] = None; "
        "import scripts.export_postings"
    )
    subprocess.run([sys.executable, "-c", blocked], check=True, cwd=Path(__file__).parent.parent)


def test_dates_are_the_calendar_day_on_this_machine(monkeypatch) -> None:
    """The crawl that prompted this ran at 5pm in California, 00:02 UTC the next day.

    Dated in UTC, every posting it confirmed read as last seen "tomorrow", and
    a deadline of today read as passed a day early.
    """
    import time
    from datetime import UTC

    from scripts.export_postings import _day

    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        assert _day(datetime(2026, 10, 1, 0, 2, tzinfo=UTC)) == date(2026, 9, 30)
    finally:
        monkeypatch.undo()
        time.tzset()
