"use client";

import { useState, useTransition } from "react";
import { splitListing } from "./actions";

export function SplitButton({ postingId, viewing }: { postingId: string; viewing: string }) {
  const [pending, start] = useTransition();
  const [error, setError] = useState<string | null>(null);

  return (
    <span className="inline-flex items-center gap-2">
      <button
        type="button"
        disabled={pending}
        onClick={() =>
          start(async () => {
            const outcome = await splitListing(postingId, viewing);
            setError(outcome.ok ? null : outcome.message);
          })
        }
        className="rounded-[var(--radius)] border border-rule px-2.5 py-1 font-mono text-xs text-ink-soft transition-colors hover:border-stop hover:text-stop disabled:opacity-50"
      >
        {pending ? "splitting…" : "not the same job — split"}
      </button>
      {error ? <span className="text-xs text-stop">{error}</span> : null}
    </span>
  );
}
