"use client";

import { useEffect, useState } from "react";
import { Assistant } from "./assistant";

/**
 * The assistant as a dockable side panel.
 *
 * Collapsed to a tab by default. The review screen's job is one decision, and
 * a chat window sitting open beside it competes for the attention that
 * decision needs — so it stays out of the way until asked for.
 */
export function AssistantDock({ applicationId }: { applicationId?: string }) {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="fixed right-0 bottom-24 z-40 rounded-l border border-r-0 border-rule bg-paper-raised px-3 py-4 font-mono text-xs tracking-widest text-ink-soft [writing-mode:vertical-rl] transition-colors hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn"
        aria-label="Open the assistant"
      >
        💬 assistant
      </button>
    );
  }

  return (
    <aside
      aria-label="Assistant"
      className="fixed inset-y-0 right-0 z-40 flex w-full max-w-md flex-col border-l border-rule bg-paper shadow-2xl"
    >
      <div className="flex items-center justify-between border-b border-rule px-3 py-2">
        <p className="font-mono text-xs uppercase tracking-widest text-ink-soft">
          assistant{applicationId ? " · this application" : ""}
        </p>
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="rounded px-2 py-1 font-mono text-xs text-ink-faint hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn"
          aria-label="Close the assistant"
        >
          close ✕
        </button>
      </div>
      <div className="min-h-0 flex-1 p-3">
        <Assistant applicationId={applicationId} compact />
      </div>
    </aside>
  );
}
