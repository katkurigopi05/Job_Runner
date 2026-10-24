# Kaggle GPU dashboard assistant

Choose **Kaggle GPU (remote)** per question in the dashboard assistant.
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
4. Locally, from this checkout, run:

   ```sh
   .venv/bin/python -m scripts.configure_kaggle
   ```

   Paste the URL into the hidden prompt and enter the kernel ID. This stores
   `storage/kaggle-connection.json` with owner-only permissions. It does not
   edit `.env`. `KAGGLE_CONNECTION_FILE` can select another private location.
5. Select **Kaggle GPU (remote)** and ask a question. The reply names the model
   and marks it remote. The existing audit records digests and sizes only.

Requests use the authenticated Jupyter kernel channel and an installed helper;
questions are JSON data, never Python source. Prompt and answer together must
fit 4,096 tokens. Notebook execution history is disabled for these requests.
No public inference endpoint or tunnel is opened. The transport logger is
disabled because handshake URLs carry credentials.

## End or resume

Stopping the Kaggle session ends GPU usage and invalidates the connection.
A new session requires reloading the model, reinstalling the helper, and
refreshing the saved URL and kernel ID. An expired session returns an error;
it never loads a local model. The saved Kaggle model dataset is unaffected.
To release model memory within a running session, delete `jobrunner_complete`,
`model` and `tokenizer`, then collect garbage and empty the CUDA cache. Stop
the session as well when finished to avoid spending GPU quota.
