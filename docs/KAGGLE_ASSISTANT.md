# Kaggle GPU dashboard assistant

Choose **Kaggle GPU (remote)** per question in the dashboard assistant.
**Auto requires Qwen3.8 27B Q6_K.** It never selects or falls back to Qwen3 8B.
If 27B is unavailable, setup reports an error instead of substituting a model.
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

## Quick start: one command per session

The connection card shows only the saved, ready notebook, named for its loaded
model (for example, **Current notebook — Qwen3.8 27B Q6_K**). Reconnecting the
same session keeps that choice. For a new session with several kernels, a single
ready 27B notebook is selected automatically; otherwise the picker uses
readable model labels. Empty kernels are hidden when a ready choice exists.
Readiness checks do not generate answers or load weights.

**Load Kaggle helper** stays visible when ready. It reuses a healthy helper and
can install the matching helper over an already loaded, identity-verified
Qwen3.8 27B UD-Q6_K engine. On a fresh session it loads 27B from the saved,
version-pinned private dataset after checking the startup script's SHA-256.
An idle 8B engine is unloaded first; its downloaded files are retained.
If any kernel is busy, wait for it to finish before switching models.

1. Start the Kaggle notebook session with **two T4 GPUs** and access to the
   saved private dataset `example-dev/qwen38-27b-q6-private`.
   Running cells is not needed.
2. In Kaggle, **Run → Kaggle Jupyter Server**, copy the **VSCode Compatible URL**.
3. In your own terminal (the prompt is hidden, so it needs a real terminal),
   run `make kaggle-setup`. When Jobrunner has no working link it asks for this
   one; it checks the session answers, and saves nothing unless setup succeeds.
   (Reconnect in the dashboard assistant saves a link too.) Never paste the link
   in chat. First-time loading can take several minutes:
   - verified 27B is reused, installing the current helper only if needed;
   - otherwise idle 8B engines are unloaded and the saved 27B bundle is loaded
     in the saved kernel, or a new kernel if needed;
   - the saved connection is changed only after 27B passes readiness. A failed
     load never falls back to 8B. Existing kernels are retained by default.
     `make kaggle-setup fresh=1` explicitly deletes every kernel first.

   It prints progress only, never the link or a kernel ID. Don't restart that
   kernel afterwards: a restart clears the model and changes the kernel ID.

The manual steps below do the same by hand.

## Connect a session

1. Start a private Kaggle notebook with two T4 GPUs and run the pinned
   saved-27B startup cell under **Reuse the saved Qwen3.8 27B model** below.
2. The bundle installs the 27B helper. To identify its kernel, run:

   ```python
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
   does not mean that the model is loaded. Click **Load Kaggle helper** to reuse
   loaded 27B model or load its saved bundle and install the current helper.
   Progress appears in the panel; first-time loading can take
   several minutes. Setup continues if you close the panel, and reopening it
   resumes progress. Wait for **Kaggle ready** before
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
fit the loaded model's context: 16,384 tokens for Qwen3.8 27B, 4,096 for
Qwen3 8B. An answer is at most 1,024 tokens. Notebook execution history is
disabled for these requests.
No public inference endpoint or tunnel is opened. The transport logger is
disabled because handshake URLs carry credentials.

Before sending a private prompt, Jobrunner checks the loaded helper's protocol,
model checksum, two-GPU identity, thinking-off setting and 16,384-token context. This check
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
To release model memory, stop the notebook session when finished to avoid
spending GPU quota. Saved dataset files remain available for the next session.

## Reuse the saved Qwen3.8 27B model

The private dataset `example-dev/qwen38-27b-q6-private` stores the pinned Q6_K
weights, tokenizer, CUDA wheels, Jobrunner helper and startup script. In a
notebook session with **two T4 GPUs**, run:

```python
from pathlib import Path
import hashlib
import kagglehub

QWEN38_BUNDLE_DIR = kagglehub.dataset_download("example-dev/qwen38-27b-q6-private/versions/1")
source = Path(QWEN38_BUNDLE_DIR, "load_qwen38.py").read_bytes()
expected = "cce702da384b9f726dce46c15e227d9174b93945a4219728f6fc0300ea024fc1"
if hashlib.sha256(source).hexdigest() != expected:
    raise RuntimeError("Saved 27B startup checksum differs")
exec(compile(source, "saved_qwen38_loader", "exec"))
```

Inside Kaggle, `dataset_download` attaches the saved dataset as notebook input;
it does not fetch the weights from Hugging Face. The startup script verifies
the saved checksums and installs the bundled CUDA wheels without accessing
PyPI. It reuses a healthy loaded 27B model, or unloads Qwen3 8B in the same
kernel before loading 27B. Other kernels holding GPU memory must be stopped
separately. Downloaded 8B files are retained.

**This cell loads 27B at the 4,096-token context the saved script states, and
Jobrunner now asks for 16,384** (2026-11-12). `make kaggle-setup` and **Load
Kaggle helper** run the same verified script with the context and helper of
this checkout (`packages/llm/kaggle_qwen27.py`, `load_27b`), so use those. A
kernel that already holds a 4,096 load is reported as not ready:
`make kaggle-setup fresh=1` replaces it.

Measured on two T4s (layers split 45/55) on 2026-11-12, with 27B loaded:

| Context | GPU 0 used / free | GPU 1 used / free |
|---|---|---|
| 4,096 | 8,683 / 6,229 MiB | 11,749 / 3,163 MiB |
| 16,384 | 9,051 / 5,861 MiB | 12,211 / 2,701 MiB |

The larger context cost 830 MiB in all, about 0.07 MiB a token, which is a
quarter of what the model file's settings predict (65 blocks, 4 KV heads,
256-wide keys and values, 16-bit). The reload took 72 s. A synthetic prompt of
8,170 tokens was answered in 22 s and one of 15,271 tokens in 42 s, both
correctly, and one over the limit was refused in 1 s without reaching the
model. Sizes above 16,384 were not tried.

After a session restart, run `make kaggle-setup`, or reconnect in the
dashboard and press **Load Kaggle helper**.
The dashboard and `make kaggle-setup` use this pinned bundle automatically.
The old 8B setup is available only to explicit library callers; it is never
Auto's choice or recovery path. Merely putting files
in `/tmp` does not save them across sessions.

The dataset names above are synthetic examples. Set `KAGGLE_QWEN27_DATASET` in your private settings to your own versioned bundle before using saved 27B startup; no account-specific default is shipped.
