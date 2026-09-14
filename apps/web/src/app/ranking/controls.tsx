"use client";

import { useState, useTransition } from "react";
import type { RankingKind, RankingSuggestion } from "@/lib/api";
import { removeAdjustment, saveAdjustment } from "./actions";

const KINDS: { value: RankingKind; label: string; placeholder: string }[] = [
  { value: "skill", label: "skill", placeholder: "python" },
  { value: "company", label: "company", placeholder: "Stripe" },
  { value: "title_term", label: "word in title", placeholder: "platform" },
  { value: "location_term", label: "word in location", placeholder: "Austin" },
  { value: "remote", label: "remote / on-site", placeholder: "remote or onsite" },
];

const FIELD =
  "rounded-[var(--radius)] border border-rule bg-paper px-3 py-1.5 text-sm focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn";

export function AddAdjustment() {
  const [kind, setKind] = useState<RankingKind>("skill");
  const [value, setValue] = useState("");
  const [weight, setWeight] = useState(0.1);
  const [message, setMessage] = useState<string | null>(null);
  const [pending, start] = useTransition();

  const placeholder = KINDS.find((k) => k.value === kind)?.placeholder ?? "";

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        start(async () => {
          const outcome = await saveAdjustment({ kind, value, weight });
          setMessage(outcome.ok ? "saved" : outcome.message);
          if (outcome.ok) setValue("");
        });
      }}
      className="flex flex-wrap items-end gap-3"
    >
      <label className="flex flex-col gap-1 text-xs text-ink-soft">
        kind
        <select value={kind} onChange={(event) => setKind(event.target.value as RankingKind)} className={FIELD}>
          {KINDS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-1 text-xs text-ink-soft">
        value
        <input value={value} onChange={(event) => setValue(event.target.value)} placeholder={placeholder} className={`w-44 ${FIELD}`} />
      </label>
      <label className="flex flex-col gap-1 text-xs text-ink-soft">
        adjustment <span className="font-mono tabular-nums text-ink">{weight >= 0 ? "+" : ""}{Math.round(weight * 100)}</span>
        <input
          type="range"
          min={-0.3}
          max={0.3}
          step={0.05}
          value={weight}
          onChange={(event) => setWeight(Number(event.target.value))}
          className="w-44 accent-[var(--color-go)]"
        />
      </label>
      <button
        type="submit"
        disabled={pending}
        className="rounded-[var(--radius)] border border-go px-3 py-1.5 text-xs text-go transition-colors hover:bg-go hover:text-paper disabled:opacity-50"
      >
        {pending ? "saving…" : "add adjustment"}
      </button>
      {message ? <span role="status" className="text-xs text-ink-soft">{message}</span> : null}
    </form>
  );
}

export function RemoveAdjustment({ id }: { id: string }) {
  const [pending, start] = useTransition();
  return (
    <button
      type="button"
      disabled={pending}
      onClick={() => start(async () => void (await removeAdjustment(id)))}
      className="font-mono text-xs text-ink-faint hover:text-stop disabled:opacity-50"
    >
      {pending ? "removing…" : "remove"}
    </button>
  );
}

export function AcceptSuggestion({ suggestion }: { suggestion: RankingSuggestion }) {
  const [pending, start] = useTransition();
  const [message, setMessage] = useState<string | null>(null);
  return (
    <span className="inline-flex items-center gap-2">
      <button
        type="button"
        disabled={pending}
        onClick={() =>
          start(async () => {
            const outcome = await saveAdjustment({ ...suggestion, source: "suggestion" });
            setMessage(outcome.ok ? null : outcome.message);
          })
        }
        className="rounded-[var(--radius)] border border-rule px-2.5 py-1 font-mono text-xs text-ink-soft transition-colors hover:border-go hover:text-go disabled:opacity-50"
      >
        {pending ? "adding…" : "accept"}
      </button>
      {message ? <span className="text-xs text-stop">{message}</span> : null}
    </span>
  );
}
