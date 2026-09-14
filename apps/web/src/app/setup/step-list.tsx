"use client";

import { useState } from "react";

/**
 * Recovery steps, each copyable.
 *
 * A step is a command or an edit. Retyping `make registry-sync dry=1` from a
 * phone screen into a terminal is where a recovery goes wrong, so each line
 * copies exactly what is shown. A trailing `# comment` is kept: it says what
 * the command does, and a shell ignores it.
 */
export function StepList({ steps }: { steps: string[] }) {
  const [copied, setCopied] = useState<number | null>(null);

  async function copy(index: number, text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(index);
      window.setTimeout(() => setCopied(null), 1500);
    } catch {
      setCopied(null);
    }
  }

  return (
    <ol className="mt-4 space-y-2">
      {steps.map((step, index) => (
        <li key={`${index}-${step}`} className="flex items-start gap-3">
          <span className="mt-2 w-5 shrink-0 font-mono text-xs tabular-nums text-ink-faint">
            {index + 1}.
          </span>
          <code className="min-w-0 flex-1 overflow-x-auto whitespace-pre-wrap break-words rounded-[var(--radius)] border border-rule-soft bg-paper px-3 py-2 font-mono text-[13px] leading-relaxed text-ink">
            {step}
          </code>
          <button
            type="button"
            onClick={() => copy(index, step)}
            aria-label={`Copy step ${index + 1}`}
            className="mt-1 shrink-0 rounded-[var(--radius)] border border-rule px-2.5 py-1 font-mono text-xs text-ink-soft transition-colors hover:border-go hover:text-go focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-go"
          >
            {copied === index ? "copied" : "copy"}
          </button>
        </li>
      ))}
    </ol>
  );
}
