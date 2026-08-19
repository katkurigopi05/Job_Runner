"use client";

import { useState } from "react";

/**
 * One answer, with a button that copies it.
 *
 * The whole point of the handoff screen is that the owner retypes nothing.
 * Selecting text out of a table by hand on twenty fields is exactly the
 * tedium that makes people skip the form and skip the application.
 */
export function CopyRow({ question, value }: { question: string; value: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard access can be refused. The value is on screen either way,
      // so this degrades to selecting it by hand rather than to nothing.
      setCopied(false);
    }
  }

  return (
    <li className="flex items-start justify-between gap-4 border-b border-rule-soft py-3 last:border-0">
      <div className="min-w-0">
        <div className="font-mono text-xs text-ink-faint">{question}</div>
        <div className="mt-1 break-words text-sm text-ink">{value}</div>
      </div>
      <button
        type="button"
        onClick={copy}
        className="shrink-0 rounded-[var(--radius)] border border-rule px-3 py-1.5 font-mono text-xs text-ink-soft transition-colors hover:border-go hover:text-go focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-go"
        aria-label={`Copy ${question}`}
      >
        {copied ? "copied" : "copy"}
      </button>
    </li>
  );
}
