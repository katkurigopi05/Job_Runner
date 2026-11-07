"use client";

import { useEffect, useState } from "react";
import { Icon } from "./icon";
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
        className="fixed right-0 bottom-24 z-40 rounded-l border border-r-0 border-rule bg-paper-raised px-3 py-4 text-xs text-ink-soft inline-flex items-center gap-2 transition-colors hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink-soft"
        aria-label="Open the assistant"
      >
        <Icon name="assistant" /> Assistant
      </button>
    );
  }

  return (
    <aside
      aria-label="Assistant"
      className="fixed inset-y-0 right-0 z-40 flex w-full max-w-md flex-col border-l border-rule bg-paper shadow-2xl"
    >
      <div className="flex items-center justify-between border-b border-rule px-3 py-2">
        <p className="text-label text-ink-soft">
          Assistant{applicationId ? " · this application" : ""}
        </p>
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="rounded-lg px-2 py-1 text-xs text-ink-faint hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink-soft"
          aria-label="Close the assistant"
        >
          <Icon name="close" />
        </button>
      </div>
      <div className="min-h-0 flex-1 p-3">
        <Assistant applicationId={applicationId} />
      </div>
    </aside>
  );
}
