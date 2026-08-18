"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useCallback } from "react";

/* Filter state lives in the URL, not in component state. A search worth
   running twice is worth being able to bookmark, and the back button should
   undo a filter rather than leave the page. */

const SENIORITY = ["intern", "junior", "mid", "senior", "staff", "principal"];

export function FilterBar({ resultCount }: { resultCount: number }) {
  const router = useRouter();
  const params = useSearchParams();

  const set = useCallback(
    (key: string, value: string) => {
      const next = new URLSearchParams(params.toString());
      if (value) next.set(key, value);
      else next.delete(key);
      router.replace(next.toString() ? `/matches?${next}` : "/matches", { scroll: false });
    },
    [params, router],
  );

  const value = (key: string) => params.get(key) ?? "";
  const active = Array.from(params.keys()).length > 0;

  return (
    <div className="rounded-[var(--radius-lg)] border border-rule bg-paper-raised p-4">
      <div className="flex flex-wrap items-end gap-4">
        <label className="flex flex-col gap-1">
          <span className="font-mono text-xs uppercase tracking-widest text-ink-soft">
            keywords
          </span>
          <input
            defaultValue={value("keywords")}
            onBlur={(event) => set("keywords", event.target.value.trim())}
            onKeyDown={(event) => {
              if (event.key === "Enter") set("keywords", event.currentTarget.value.trim());
            }}
            placeholder="python, postgres"
            className="w-48 rounded-[var(--radius)] border border-rule bg-paper px-3 py-1.5 text-sm focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn"
          />
        </label>

        <label className="flex flex-col gap-1">
          <span className="font-mono text-xs uppercase tracking-widest text-ink-soft">
            location
          </span>
          <input
            defaultValue={value("locations")}
            onBlur={(event) => set("locations", event.target.value.trim())}
            onKeyDown={(event) => {
              if (event.key === "Enter") set("locations", event.currentTarget.value.trim());
            }}
            placeholder="austin, berlin"
            className="w-44 rounded-[var(--radius)] border border-rule bg-paper px-3 py-1.5 text-sm focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn"
          />
        </label>

        <label className="flex flex-col gap-1">
          <span className="font-mono text-xs uppercase tracking-widest text-ink-soft">
            remote
          </span>
          <select
            value={value("remote")}
            onChange={(event) => set("remote", event.target.value)}
            className="rounded-[var(--radius)] border border-rule bg-paper px-3 py-1.5 text-sm focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn"
          >
            <option value="">either</option>
            <option value="true">remote only</option>
            <option value="false">on-site only</option>
          </select>
        </label>

        <label className="flex flex-col gap-1">
          <span className="font-mono text-xs uppercase tracking-widest text-ink-soft">
            at least
          </span>
          <select
            value={value("min_seniority")}
            onChange={(event) => set("min_seniority", event.target.value)}
            className="rounded-[var(--radius)] border border-rule bg-paper px-3 py-1.5 text-sm focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn"
          >
            <option value="">any level</option>
            {SENIORITY.map((level) => (
              <option key={level} value={level}>
                {level}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1">
          <span className="font-mono text-xs uppercase tracking-widest text-ink-soft">
            seen within
          </span>
          <select
            value={value("posted_within_days")}
            onChange={(event) => set("posted_within_days", event.target.value)}
            className="rounded-[var(--radius)] border border-rule bg-paper px-3 py-1.5 text-sm focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn"
          >
            <option value="">any time</option>
            <option value="1">24 hours</option>
            <option value="7">7 days</option>
            <option value="30">30 days</option>
          </select>
        </label>

        {active ? (
          <button
            type="button"
            onClick={() => router.replace("/matches", { scroll: false })}
            className="rounded-[var(--radius)] border border-rule px-3 py-1.5 font-mono text-xs text-ink-soft transition-colors hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn"
          >
            clear
          </button>
        ) : null}

        <span className="ml-auto font-mono text-xs tabular-nums text-ink-faint">
          {resultCount} shown
        </span>
      </div>

      <p className="mt-3 max-w-prose font-mono text-xs text-ink-faint">
        These filter what you see. They do not touch your profile, which is what gets typed
        into application forms.
      </p>
    </div>
  );
}
