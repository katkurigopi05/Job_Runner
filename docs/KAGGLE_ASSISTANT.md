# Kaggle GPU dashboard assistant

Choose **Kaggle GPU (auto, remote)** per question in the dashboard assistant.
The default remains local Ollama. Kaggle receives the question and retrieved
application/profile/skill context. Recruiter mail and mail-bearing history
remain withheld unless **share mail** is enabled for that question. Protected
profile questions are still refused in code. Nothing falls back to another model.

The shared cloud guard masks known local profile/résumé names and email/phone
patterns before the request reaches Kaggle, then restores exact returned
placeholders locally. Identifying URLs are masked when they contain these
contacts. Other identifying prose can remain; this does not anonymize the
context or replace the separate mail-consent choice. The mapping is held only
in local memory, not sent to the notebook, logged or persisted.

## Auto model selection

The connection panel checks the helpers already loaded in the selected kernel,
without generating an answer or loading weights. It supports:

| Loaded model | Helper | Readiness requirements |
|---|---|---|
| Qwen3.8 27B UD-Q6_K | `jobrunner_qwen38_complete` | Pinned artifact SHA-256, two GPUs, thinking disabled, 4,096-token context |
| Qwen3 8B 4-bit | `jobrunner_complete` | Existing verified protocol-1 GPU helper, 4,096-token context |

Auto prefers ready 27B, otherwise ready 8B. It binds that choice before a question
is sent and refuses a model change during the request. The status panel, reply
metadata and audit name the actual model; unsupported models are refused rather
than relabelled. This does not change the default local provider or enable
fallback during a question. An unknown model needs a reviewed helper contract
before it can join auto selection.

For an already staged and verified 27B engine, run the contents of
`scripts/kaggle_qwen38_helper.py` in its kernel, then install:

```python
jobrunner_qwen38_complete = install(
    jobrunner27_engine, jobrunner27_tokenizer, jobrunner27_identity
)
```

The identity must come from the artifact-hash and two-GPU allocation checks in
the loader, not an invented model label. The supported GGUF SHA-256 is
`c9c206812fbe4ac7b76a729e25928b63f2ae89d37f69da7a71c20aec763cd436`.
Installing this helper does not download or reload the model. `make kaggle-setup`
preserves a ready 27B helper; if the three `jobrunner27_*` globals above survive,
it can reinstall just that helper. It does not stage or download 27B weights.
After a complete session restart, stage and load 27B again before using auto;
the existing fresh-kernel setup path still loads the attached 8B dataset.

## Quick start: one command per session

1. Start the Kaggle notebook session with a GPU and the model dataset attached.
   Running cells is not needed.
2. In Kaggle, **Run → Kaggle Jupyter Server**, copy the **VSCode Compatible URL**.
3. In your own terminal (the prompt is hidden, so it needs a real terminal),
   run `make kaggle-setup`. When Jobrunner has no working link it asks for this
   one; it checks the session answers, and saves nothing unless setup succeeds.
   (Reconnect in the dashboard assistant saves a link too.) Never paste the link
   in chat. It takes about 3–4 minutes the first time in a session:
   - a kernel that is already ready is left alone, and one that already holds
     the model only gets the helper;
   - otherwise it starts a kernel, installs the libraries, finds the attached
     4-bit Qwen3 8B by its `config.json` (no path is kept in this repository),
     copies it to working disk, loads it on the GPU, and installs the helper
     sent from this checkout's `scripts/kaggle_helper.py` (nothing is
     downloaded, and there is no hash to pin);
   - it points Jobrunner at that kernel and deletes kernels that are idle with
     nothing attached, so a second copy of the model never holds GPU memory.
     A kernel running a cell, or one VS Code is attached to, is kept.
     `make kaggle-setup fresh=1` deletes every kernel first.

   It prints progress only, never the link or a kernel ID. Don't restart that
   kernel afterwards: a restart clears the model and changes the kernel ID.

The manual steps below do the same by hand.

## Connect a session

1. Start a private Kaggle GPU notebook, attach your saved model and load
   `unsloth/Qwen3-8B-bnb-4bit` into `model` and `tokenizer`. Use FP16 compute on
   the T4. For Transformers 5's loader, `HF_DEACTIVATE_ASYNC_LOAD=1` before
   loading and copying the weights from the input mount to `/kaggle/working`
   resolved the observed stalled load. Stop the old attempt before retrying.
2. Copy `scripts/kaggle_helper.py` into a notebook cell and run it. Then run:

   ```python
   jobrunner_complete = install(model, tokenizer)
   from pathlib import Path
   from ipykernel import get_connection_file
   print(Path(get_connection_file()).stem.removeprefix("kernel-"))
   ```

   Use the kernel of the **loaded model**, which can differ from the browser's
   kernel when the model was loaded from an IDE.
3. In Kaggle, **Run → Kaggle Jupyter Server**, copy the **VSCode Compatible URL**.
   This entire URL is a credential, including its path. Do not paste it in
   chat, issues, screenshots, shell command arguments or tracked files.
4. In the dashboard assistant, select **Kaggle GPU (remote)**, click
   **Reconnect**, and paste the URL into the password field. Click **Save and
   check**. If several kernels are active, choose the ID printed in step 2.
   The URL is stored privately on this machine and is never returned by the API.

   The panel distinguishes an unavailable session, a restarted kernel, a missing
   or outdated helper, and a model that is not ready. A reachable kernel alone
   does not mean that the model is loaded. Run the current helper after updating
   Jobrunner, then click **Check connection**. Wait for **Kaggle ready** before
   asking model questions. Factual answers that do not use a model still work.

   The CLI remains available from this checkout:

   ```sh
   .venv/bin/python -m scripts.configure_kaggle
   ```

   Paste the URL into the hidden prompt. The command verifies the session and
   automatically selects its kernel when only one is active. If multiple kernels
   are active, enter the ID of the one holding the loaded model; an ID outside
   this session is refused. A failed check leaves the previous connection intact.
   The CLI verifies the active kernel; the dashboard and every model request
   also check model/helper readiness. The connection is stored with owner-only
   permissions. It does not edit `.env`. By default,
   `storage/kaggle-connection.json` belongs to this installation, regardless of
   the launch directory. With `JOBRUNNER_ENV_FILE`, relative paths belong to
   that settings file's directory, so the API and CLI must select the same
   settings file. `KAGGLE_CONNECTION_FILE` can select an absolute private path.
5. Select **Kaggle GPU (remote)** and ask a question. The reply names the model
   and marks it remote. The existing audit records digests and sizes only.

Requests use the authenticated Jupyter kernel channel and an installed helper;
questions are JSON data, never Python source. Prompt and answer together must
fit 4,096 tokens. Notebook execution history is disabled for these requests.
No public inference endpoint or tunnel is opened. The transport logger is
disabled because handshake URLs carry credentials.

Before sending a private prompt, Jobrunner checks the loaded helper's protocol,
model, CUDA/4-bit state, tokenizer interfaces and 4,096-token context. This check
does not load weights, run inference, or send a question. A failed check stops
the request with a specific recovery instruction.

## End or resume

Stopping the Kaggle session ends GPU usage and invalidates the connection.
A new session requires reloading the model, reinstalling the helper, and
refreshing the saved URL and kernel ID. Connecting an IDE to the notebook does
not refresh Jobrunner: use **Reconnect** in the assistant (or the CLI above)
with the new VSCode Compatible URL each time the session restarts, then
`make kaggle-setup`. You do not need to download the saved dataset again. Kaggle controls session expiration,
so a connection cannot be kept valid across stopped sessions.

An expired or missing kernel returns a reconnect instruction; it never loads a
local model. The saved Kaggle model dataset is unaffected.
To release model memory within a running session, delete `jobrunner_complete`,
`model` and `tokenizer`, then collect garbage and empty the CUDA cache. Stop
the session as well when finished to avoid spending GPU quota.
