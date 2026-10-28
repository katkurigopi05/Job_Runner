"use client";

import { useEffect, useId, useState } from "react";

type KaggleState =
  | "unconfigured"
  | "unreachable"
  | "expired"
  | "kernel_missing"
  | "helper_missing"
  | "helper_outdated"
  | "model_not_ready"
  | "ready"
  | "select_kernel";

interface KaggleStatus {
  state: KaggleState;
  detail: string;
  kernel_id: string | null;
  kernels: string[];
  kernel_options?: { id: string; label: string; state: KaggleState; model: string | null }[];
  model: string | null;
  context_tokens: number | null;
}

const HEADINGS: Record<KaggleState, string> = {
  unconfigured: "Connect Kaggle",
  unreachable: "Kaggle connection unavailable",
  expired: "Kaggle session expired",
  kernel_missing: "Kaggle notebook unavailable",
  helper_missing: "Load the Kaggle helper",
  helper_outdated: "Update the Kaggle helper",
  model_not_ready: "Load the Kaggle model",
  ready: "Kaggle ready",
  select_kernel: "Choose a Kaggle notebook",
};

const BUTTON =
  "rounded border border-rule px-2.5 py-1 text-xs text-ink-soft transition-colors hover:border-attn hover:text-ink disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn";
const INPUT =
  "min-w-0 rounded border border-rule bg-paper px-2 py-1.5 text-xs text-ink disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn";

async function readStatus(response: Response): Promise<KaggleStatus> {
  const body = await response.json();
  if (!response.ok) {
    throw new Error(body.error?.message ?? "Could not check the Kaggle connection.");
  }
  return body as KaggleStatus;
}

/** A saved URL and an active model are separate parts of Kaggle readiness. */
export function KaggleConnection() {
  const id = useId();
  const [status, setStatus] = useState<KaggleStatus | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState(false);
  // The URL contains credentials. Keep it in memory only until it is saved.
  const [serverUrl, setServerUrl] = useState("");
  const [kernelId, setKernelId] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/chat/kaggle/status", { cache: "no-store", signal: controller.signal })
      .then(readStatus)
      .then((body) => {
        if (!controller.signal.aborted) setStatus(body);
      })
      .catch(() => {
        if (!controller.signal.aborted) setError("Could not check the Kaggle connection. Try again.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, []);

  async function check() {
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/chat/kaggle/status", { cache: "no-store" });
      setStatus(await readStatus(response));
    } catch (failure) {
      setStatus(null);
      setError(failure instanceof Error ? failure.message : "Could not check the Kaggle connection.");
    } finally {
      setBusy(false);
    }
  }

  async function connect() {
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/chat/kaggle/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ server_url: serverUrl.trim(), kernel_id: kernelId || null }),
      });
      const body = await readStatus(response);
      setStatus(body);
      if (body.state === "select_kernel") {
        setKernelId("");
      } else if (["ready", "helper_missing", "helper_outdated", "model_not_ready"].includes(body.state)) {
        setServerUrl("");
        setKernelId("");
        setExpanded(false);
      }
    } catch (failure) {
      setStatus(null);
      setError(failure instanceof Error ? failure.message : "Could not save the Kaggle connection.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="Kaggle connection" className="space-y-2 border-b border-rule px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <div role="status" aria-live="polite" className="min-w-0 flex-1 text-xs">
          <p className="text-ink">
            {busy ? "Checking Kaggle connection…" : status ? HEADINGS[status.state] : "Kaggle status unavailable"}
          </p>
          {!busy && status ? <p className="mt-1 text-ink-soft">{status.detail}</p> : null}
        </div>
        <button type="button" onClick={() => void check()} disabled={busy} className={BUTTON}>
          Check connection
        </button>
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={`${id}-form`}
          onClick={() => setExpanded((value) => !value)}
          disabled={busy}
          className={BUTTON}
        >
          Reconnect
        </button>
      </div>
      <p className="text-xs text-ink-faint">
        Auto uses a ready supported model in this notebook. IDE connection is separate.
        Restarting Kaggle requires reconnecting here and loading the model/helper.
      </p>
      {error ? <p role="alert" className="text-xs text-stop">{error}</p> : null}
      {expanded ? (
        <form
          id={`${id}-form`}
          className="space-y-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (!busy && serverUrl.trim()) void connect();
          }}
        >
          <div className="flex flex-col gap-1">
            <label htmlFor={`${id}-url`} className="text-xs text-ink-soft">VSCode Compatible URL</label>
            <input
              id={`${id}-url`}
              type="password"
              maxLength={2048}
              autoComplete="off"
              spellCheck={false}
              value={serverUrl}
              disabled={busy}
              onChange={(event) => {
                setServerUrl(event.target.value);
                setKernelId("");
                if (status?.state === "select_kernel") setStatus(null);
              }}
              className={INPUT}
              required
            />
          </div>
          {status?.state === "select_kernel" ? (
            <div className="flex flex-col gap-1">
              <label htmlFor={`${id}-kernel`} className="text-xs text-ink-soft">Kaggle notebook</label>
              <select
                id={`${id}-kernel`}
                value={kernelId}
                disabled={busy}
                onChange={(event) => setKernelId(event.target.value)}
                className={INPUT}
                required
              >
                <option value="">Choose a notebook</option>
                {status.kernels.map((kernel, index) => (
                  <option key={kernel} value={kernel}>
                    {status.kernel_options?.find((option) => option.id === kernel)?.label ?? `Notebook ${index + 1}`}
                  </option>
                ))}
              </select>
            </div>
          ) : null}
          <button
            type="submit"
            disabled={busy || !serverUrl.trim() || (status?.state === "select_kernel" && !kernelId)}
            className={BUTTON}
          >
            Save and check
          </button>
        </form>
      ) : null}
    </section>
  );
}
