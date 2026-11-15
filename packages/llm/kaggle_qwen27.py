"""Pinned saved 27B startup cells. Retain both models' files on Kaggle disk."""

UNLOAD_8B = """
import gc
import torch
def _jobrunner_release_8b():
    loaded = globals().get('model')
    if loaded is not None:
        assert loaded.config.model_type == 'qwen3'
        assert loaded.config.hidden_size == 4096
        assert loaded.config.num_hidden_layers == 36
        aliases = [name for name, value in list(globals().items()) if value is loaded]
        for name in ['jobrunner_complete', 'tokenizer', *aliases]:
            globals().pop(name, None)
    else:
        assert not callable(globals().get('jobrunner_complete'))
_jobrunner_release_8b()
gc.collect()
torch.cuda.empty_cache()
torch.cuda.synchronize()
assert 'model' not in globals()
assert not callable(globals().get('jobrunner_complete'))
print('JOBRUNNER_8B_UNLOADED', flush=True)
"""

#: The saved loader, pinned. It is run only when its bytes are these.
LOADER_SHA256 = "cce702da384b9f726dce46c15e227d9174b93945a4219728f6fc0300ea024fc1"

_LOAD_27B = """
from pathlib import Path
import hashlib
import kagglehub
assert 'model' not in globals()
assert not callable(globals().get('jobrunner_complete'))
QWEN38_BUNDLE_DIR = kagglehub.dataset_download({dataset!r})
source = Path(QWEN38_BUNDLE_DIR, 'load_qwen38.py').read_bytes()
if hashlib.sha256(source).hexdigest() != {loader_sha256!r}:
    raise RuntimeError('Saved Qwen 27B startup checksum differs; refusing to execute it.')
loader = source.decode('utf-8')
for saved, wanted in {edits!r}:
    if loader.count(saved) != 1:
        raise RuntimeError('Saved Qwen 27B loader lacks a setting; refusing to change it.')
    loader = loader.replace(saved, wanted)
JOBRUNNER_QWEN38_HELPER = {helper_source!r}
try:
    exec(compile(loader, 'saved_qwen38_loader', 'exec'))
finally:
    del JOBRUNNER_QWEN38_HELPER
print('Loaded Qwen 27B from the saved Kaggle dataset.', flush=True)
"""


def load_27b(helper_source: str, *, loader_sha256: str = LOADER_SHA256) -> str:
    """The startup cell: the saved loader, at this checkout's context and helper.

    The saved loader is a file in the owner's Kaggle dataset beside 22 GB of
    weights, and it states a 4,096-token context in three places: the engine,
    the identity it reports, and the helper it installs from the bundle. A new
    dataset version would upload the weights again to change three numbers, so
    the cell changes them in the text it has just verified. Each must appear
    exactly once, because the pinned bytes are what make the edit exact; a
    loader that differs is refused rather than run at a context nobody chose.
    """
    from packages.core.config import get_settings
    from packages.llm.kaggle import CONTEXT_TOKENS, QWEN38_MODEL
    from packages.llm.provider import LLMError

    dataset = get_settings().kaggle_qwen27_dataset
    if not dataset:
        raise LLMError("Set KAGGLE_QWEN27_DATASET to your versioned private bundle before setup.")

    context = CONTEXT_TOKENS[QWEN38_MODEL]
    edits = (
        ("n_ctx=4096,", f"n_ctx={context},"),
        ("context_tokens=4096,", f"context_tokens={context},"),
        ("(bundle / 'qwen38_helper.py').read_text()", "globals()['JOBRUNNER_QWEN38_HELPER']"),
    )
    return _LOAD_27B.format(
        dataset=dataset, loader_sha256=loader_sha256, edits=edits, helper_source=helper_source
    )
