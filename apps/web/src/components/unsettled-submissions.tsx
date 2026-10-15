"use client";

import { useState, useTransition } from "react";
import type { UnsettledSubmission } from "@/lib/api";
import { recordNotSubmitted, recordSubmitted } from "@/app/finish/actions";
import { openExternal } from "@/lib/external";

/**
 * Attempts to send that nobody has vouched for, one answer each.
 *
 * Each row stops its job being applied to again, so it is listed until the
 * owner says, after checking with the employer, whether it arrived. Either
 * answer is a record of what they found; neither sends anything.
 */
export function UnsettledSubmissions({ initial }: { initial: UnsettledSubmission[] }) {
  const [rows, setRows] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  if (rows.length === 0) return null;

  const settle = (id: string, sent: boolean) =>
    startTransition(async () => {
      setError(null);
      const result = sent ? await recordSubmitted(id) : await recordNotSubmitted(id);
      if (!result.ok) {
        setError(result.message);
        return;
      }
      setRows((current) => current.filter((row) => row.application_id !== id));
    });

  return (
    <section aria-labelledby="unsettled-heading" className="space-y-4">
      <div className="space-y-2">
        <h2 id="unsettled-heading" className="font-display text-2xl leading-tight">
          Check with the employer first
        </h2>
        <p className="max-w-2xl text-sm text-ink-soft">
          A send was attempted for these and nobody knows whether it arrived. Until you say,
          each one keeps its job from being applied to again. Look for a confirmation email or
          the employer&rsquo;s portal, then answer.
        </p>
      </div>

      <ul className="space-y-3">
        {rows.map((row) => (
          <li
            key={row.application_id}
            className="rounded-[var(--radius)] border-l-2 border-attn bg-attn-soft/40 px-4 py-3"
          >
            <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
              <span className="font-medium">
                {row.title ?? row.url}
                {row.company ? <span className="text-ink-soft"> at {row.company}</span> : null}
              </span>
              <span className="font-mono text-xs text-ink-faint">
                {row.state}
                {row.source === "legacy" ? ", from before the ledger" : ""}
                {row.attempted_at ? `, ${new Date(row.attempted_at).toLocaleDateString()}` : ""}
              </span>
            </div>
            <div className="mt-3 flex flex-wrap gap-3">
              <button
                type="button"
                onClick={() => openExternal(row.url)}
                className="rounded-[var(--radius)] border border-rule px-3 py-1.5 font-mono text-xs text-ink-soft transition-colors hover:border-go hover:text-go"
              >
                open posting
              </button>
              <button
                type="button"
                disabled={pending}
                onClick={() => settle(row.application_id, true)}
                className="rounded-[var(--radius)] border border-go px-3 py-1.5 font-mono text-xs text-go transition-colors hover:bg-go hover:text-on-accent disabled:opacity-40"
              >
                it was sent
              </button>
              <button
                type="button"
                disabled={pending}
                onClick={() => settle(row.application_id, false)}
                className="rounded-[var(--radius)] border border-rule px-3 py-1.5 font-mono text-xs text-ink-soft transition-colors hover:border-ink-soft disabled:opacity-40"
              >
                it was not sent
              </button>
            </div>
          </li>
        ))}
      </ul>
      {error ? <p className="font-mono text-xs text-stop">{error}</p> : null}
    </section>
  );
}
