"""The Colab batch notebook embeds as the whole index was, and names what the commands expect.

New vectors are compared with 223,576 made on 2026-11-02, so any drift in the
model, precision, padding, pooling or normalisation would make new postings
rank on a different scale from old ones, silently. And the notebook reads and
writes files the export and import commands name, so a rename in one place
would leave the owner with a file nothing reads.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from scripts.export_pending_chunks import EXPORT_NAME, VECTORS_NAME

NOTEBOOK = Path(__file__).resolve().parent.parent / "scripts" / "colab" / "qwen_embed_batch.ipynb"


def _code() -> list[str]:
    cells = json.loads(NOTEBOOK.read_text())["cells"]
    return ["".join(cell["source"]) for cell in cells if cell["cell_type"] == "code"]


def _script() -> str:
    (written,) = [cell for cell in _code() if cell.startswith("%%writefile")]
    return written.split("\n", 1)[1]


def test_every_code_cell_is_valid_python():
    for cell in _code():
        ast.parse(cell.split("\n", 1)[1] if cell.startswith("%%") else cell)


def test_it_embeds_as_the_whole_index_was_embedded():
    script = _script()
    assert 'MODEL = "Qwen/Qwen3-Embedding-4B"' in script
    assert "torch.bfloat16" in script
    assert 'padding_side="left"' in script
    assert "hidden[:, -1, :]" in script, "last-token pooling"
    assert "normalize(" in script and "p=2" in script
    assert "max_length=512" in script
    assert "Instruct:" not in script, "postings are embedded without the question instruction"


def test_it_reads_and_writes_the_files_the_commands_name():
    source = "\n".join(_code())
    assert f'"{EXPORT_NAME}"' in source
    assert VECTORS_NAME in source


def test_it_carries_no_outputs():
    cells = json.loads(NOTEBOOK.read_text())["cells"]
    assert all(not cell.get("outputs") for cell in cells if cell["cell_type"] == "code")
