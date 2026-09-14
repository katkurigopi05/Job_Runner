"use client";

import { useState, useTransition } from "react";
import type { RegistrySyncResult } from "@/lib/api";
import { syncRegistry } from "./actions";

/**
 * Preview, then apply, the registry repair.
 *
 * Apply is disabled until a preview has run in this view, so the owner sees
 * what will change before it changes. It stays a click rather than something
 * this page does on load: applying makes boards fetchable, and the next crawl
 * tick sends real requests to those employers.
 */
export function RegistrySync() {
  const [preview, setPreview] = useState<RegistrySyncResult | null>(null);
  const [applied, setApplied] = useState<RegistrySyncResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  function run(dryRun: boolean) {
    setError(null);
    startTransition(async () => {
      const outcome = await syncRegistry(dryRun);
      if (!outcome.ok) {
        setError(outcome.message);
        return;
      }
      if (dryRun) {
        setPreview(outcome.result);
        setApplied(null);
      } else {
        setApplied(outcome.result);
        setPreview(null);
      }
    });
  }

  return (
    <div className="mt-5 rounded-[var(--radius)] border border-rule-soft bg-paper p-4">
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          disabled={pending}
          onClick={() => run(true)}
          className="rounded-[var(--radius)] border border-rule px-3 py-1.5 font-mono text-xs text-ink transition-colors hover:border-go hover:text-go disabled:opacity-50"
        >
          {pending && !preview ? "previewing…" : "Preview repair"}
        </button>
        <button
          type="button"
          disabled={pending || preview === null}
          onClick={() => run(false)}
          className="rounded-[var(--radius)] border border-go bg-go px-3 py-1.5 font-mono text-xs text-paper transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:border-rule disabled:bg-transparent disabled:text-ink-faint"
        >
          {pending && preview ? "applying…" : "Apply repair"}
        </button>
        <span className="text-xs text-ink-faint">
          Applying makes these boards fetchable; the next crawl polls them.
        </span>
      </div>

      <div aria-live="polite" className="mt-3 text-sm">
        {error ? <p className="text-stop">{error}</p> : null}
        {preview ? (
          <p className="text-ink-soft">
            <span className="font-mono text-xs uppercase tracking-widest text-attn">
              preview · nothing written
            </span>
            <br />
            {preview.summary}
          </p>
        ) : null}
        {applied ? (
          <p className="text-ink-soft">
            <span className="font-mono text-xs uppercase tracking-widest text-go">applied</span>
            <br />
            {applied.summary}
          </p>
        ) : null}
      </div>
    </div>
  );
}
