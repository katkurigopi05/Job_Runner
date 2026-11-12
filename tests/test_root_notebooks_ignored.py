"""A scratch notebook at the repo root must not fail `make lint`.

Two notebooks saved from Kaggle sessions sat untracked at the root, one of
them an empty file that is not valid JSON. Ruff lints notebooks, so `make
lint` and gate-0 failed on the owner's machine and nowhere else. Ruff skips
what git ignores, so the root is ignored and `scripts/` is not.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _is_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", path], cwd=ROOT, check=False
    )
    return result.returncode == 0


def test_notebook_at_the_root_is_ignored() -> None:
    assert _is_ignored("scratch.ipynb")


def test_project_notebooks_are_still_tracked_and_linted() -> None:
    assert not _is_ignored("scripts/colab/qwen_embed_batch.ipynb")
    assert not _is_ignored("scripts/kaggle/qwen_embed_batch_gpu.ipynb")
