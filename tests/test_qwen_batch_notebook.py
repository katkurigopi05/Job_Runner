"""The batch notebooks embed as the whole index was, and name what the commands expect.

New vectors are compared with 223,576 made on 2026-11-02, so any drift in the
model, padding, pooling or normalisation would make new postings rank on a
different scale from old ones, silently. And a notebook reads and writes files
the export and import commands name, so a rename in one place would leave the
owner with a file nothing reads.

Two notebooks: a Colab TPU in bfloat16, as the whole index was made, and a
Kaggle GPU in float16, since a T4 or P100 has no native bfloat16. The import
compares re-made vectors with stored ones before writing (`MIN_AGREEMENT`).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from scripts.export_pending_chunks import EXPORT_NAME, VECTORS_NAME

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
NOTEBOOKS = {
    "colab": (SCRIPTS / "colab" / "qwen_embed_batch.ipynb", "torch.bfloat16"),
    "kaggle": (SCRIPTS / "kaggle" / "qwen_embed_batch_gpu.ipynb", "torch.float16"),
}

pytestmark = pytest.mark.parametrize("name", list(NOTEBOOKS))


def _cells(name: str) -> list[dict]:
    return json.loads(NOTEBOOKS[name][0].read_text())["cells"]


def _code(name: str) -> list[str]:
    return ["".join(cell["source"]) for cell in _cells(name) if cell["cell_type"] == "code"]


def _script(name: str) -> str:
    (written,) = [cell for cell in _code(name) if cell.startswith("%%writefile")]
    return written.split("\n", 1)[1]


def test_every_code_cell_is_valid_python(name):
    for cell in _code(name):
        ast.parse(cell.split("\n", 1)[1] if cell.startswith("%%") else cell)


def test_it_embeds_as_the_whole_index_was_embedded(name):
    script = _script(name)
    assert 'MODEL = "Qwen/Qwen3-Embedding-4B"' in script
    assert NOTEBOOKS[name][1] in script
    assert 'padding_side="left"' in script
    assert "hidden[:, -1, :]" in script, "last-token pooling"
    assert "normalize(" in script and "p=2" in script
    assert "max_length=512" in script
    assert "Instruct:" not in script, "postings are embedded without the question instruction"


def test_it_reads_and_writes_the_files_the_commands_name(name):
    source = "\n".join(_code(name))
    assert f'"{EXPORT_NAME}"' in source
    assert VECTORS_NAME in source


def test_it_carries_no_outputs(name):
    assert all(not cell.get("outputs") for cell in _cells(name) if cell["cell_type"] == "code")
