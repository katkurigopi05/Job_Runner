"use client";

import { useEffect, useState } from "react";
import type { Health } from "@/lib/api";

/** How often to re-check. Slow: this is a local process, not a remote service. */
const POLL_MS = 15_000;

type State = Health | "unreachable" | "checking";

/**
 * Whether the machine behind this dashboard is actually working.
 *
 * Three states worth distinguishing, because the fix differs for each:
 *
 * - **unreachable** — the API is not answering at all. `make api`.
 * - **degraded** — the API answers but cannot reach Postgres. `make up`.
 * - **ok** — both.
 *
 * Without this, all three look the same from the dashboard: pages throw and the
 * owner guesses which of the two processes stopped. Reading it through the same
 * Next rewrite everything else uses, so the API keeps refusing non-loopback
 * callers and needs no CORS.
 */
export function ApiHealth() {
  const [state, setState] = useState<State>("checking");

  useEffect(() => {
    let live = true;

    const check = async () => {
      try {
        const res = await fetch("/api/health", { cache: "no-store" });
        if (!res.ok) throw new Error(String(res.status));
        const body = (await res.json()) as Health;
        if (live) setState(body);
      } catch {
        // Any failure to get an answer is the same fact: nothing is listening.
        if (live) setState("unreachable");
      }
    };

    void check();
    const timer = setInterval(check, POLL_MS);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, []);

  if (state === "checking") return null;

  const { label, tone, title } =
    state === "unreachable"
      ? {
          label: "api down",
          tone: "text-stop",
          title: "The API is not answering. Start it with: make api",
        }
      : state.database !== "ok"
        ? {
            label: "db down",
            tone: "text-attn",
            title: "The API is up but cannot reach Postgres. Start it with: make up",
          }
        : { label: "localhost only", tone: "text-ink-faint", title: "API and database both up" };

  return (
    <p className={`font-mono text-xs ${tone}`} title={title} aria-live="polite">
      {label}
    </p>
  );
}
