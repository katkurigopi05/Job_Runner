"""Any module may be imported first, in a fresh interpreter.

`models.py` imports its feature modules at the end, and `models_interview`
reads `LARGE_EMBEDDING_DIM` from `models_chunks`. A process whose first import
was `models_chunks` (directly, or through `query_vectors`) reached that read
while `models_chunks` was only part-loaded, and `make chunk-postings` and
`make query-embedder-check` both died with an ImportError on 2026-11-23.
Each case runs in its own interpreter because a module already imported by
the test session would hide the order.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FIRST_IMPORTS = (
    "packages.core.models_chunks",
    "packages.core.models_interview",
    "packages.matching.query_vectors",
    "packages.matching.chunk_vectors",
    "scripts.chunk_postings",
    "scripts.check_query_embedder",
)


@pytest.mark.parametrize("module", FIRST_IMPORTS)
def test_the_module_imports_first_in_a_fresh_interpreter(module: str) -> None:
    # Act
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    # Assert
    assert result.returncode == 0, result.stderr[-2000:]
