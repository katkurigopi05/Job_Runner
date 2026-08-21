"use client";

import { useCallback, useEffect, useState } from "react";
import { type Decision, type Match } from "@/lib/api";
import { recordDecision } from "@/app/swipe/actions";

/**
 * One posting at a time, kept or discarded.
 *
 * The feed exists because a ranked list of 1,853 postings is not something
 * anyone reads. It is also the only way this project gets labelled data:
 * CLAUDE.md §15 says Gate 5's "hand-labeled set of 20 postings" does not
 * exist, and every swipe here is one label produced by using the tool rather
 * than by sitting down to annotate. `/matches/calibration` reads them back.
 *
 * **A swipe never applies to anything.** Right means "worth applying to",
 * and the application is still started deliberately from the posting. §2.3
 * says nothing submits without explicit approval, and a gesture this cheap
 * must not be the thing that approves.
 */
export function SwipeDeck({ initial }: { initial: Match[] }) {
  const [queue, setQueue] = useState(initial);
  const [pending, setPending] = useState<Decision | null>(null);
  const [decided, setDecided] = useState({ interested: 0, skipped: 0 });
  const [error, setError] = useState<string | null>(null);

  const current = queue[0];

  const decide = useCallback(
    async (decision: Decision) => {
      if (!current || pending) return;
      setPending(decision);
      setError(null);
      try {
        // A Server Action, not a browser fetch: the API refuses non-loopback
        // callers, so the request has to originate on the Next server.
        const result = await recordDecision(current.id, decision);
        if (!result.ok) {
          setError(result.message);
          return;
        }
        setDecided((d) => ({ ...d, [decision]: d[decision] + 1 }));
        // Drop the card only once the write succeeded. Removing it first
        // would lose the verdict silently whenever the API is down, and the
        // whole point of the deck is that the verdicts accumulate.
        setQueue((q) => q.slice(1));
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "could not save that");
      } finally {
        setPending(null);
      }
    },
    [current, pending],
  );

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "ArrowRight") void decide("interested");
      if (event.key === "ArrowLeft") void decide("skipped");
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [decide]);

  if (!current) {
    return (
      <div className="rounded-[var(--radius-lg)] border border-rule bg-paper-raised p-10 text-center">
        <p className="font-display text-2xl">Nothing left to rate</p>
        <p className="mt-3 text-sm text-ink-soft">
          {decided.interested + decided.skipped > 0
            ? `${decided.interested} kept, ${decided.skipped} skipped this session.`
            : "Run a crawl, or widen the filters on the Matches page."}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <article
        className="rounded-[var(--radius-lg)] border border-rule bg-paper-raised p-8 shadow-[var(--shadow-panel)] transition-transform"
        style={{
          transform: pending
            ? `translateX(${pending === "interested" ? "8%" : "-8%"}) rotate(${
                pending === "interested" ? "2deg" : "-2deg"
              })`
            : undefined,
        }}
      >
        <div className="flex items-start justify-between gap-6">
          <h2 className="font-display text-2xl leading-tight">{current.title ?? "Untitled role"}</h2>
          <span className="shrink-0 font-mono text-sm text-ink-faint">
            {current.score.toFixed(3)}
          </span>
        </div>

        <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 font-mono text-xs text-ink-faint">
          {current.location ? <span>{current.location}</span> : null}
          {current.ats_type ? <span>{current.ats_type}</span> : null}
        </div>

        {/* The reasoning travels with the number. A score you cannot argue
            with is one you end up ignoring. */}
        {current.matched_terms?.length ? (
          <div className="mt-6">
            <div className="font-mono text-xs uppercase tracking-widest text-ink-faint">Matched</div>
            <div className="mt-2 flex flex-wrap gap-2">
              {current.matched_terms.slice(0, 12).map((term) => (
                <span key={term} className="rounded-full border border-go/40 px-3 py-1 text-xs text-go">
                  {term}
                </span>
              ))}
            </div>
          </div>
        ) : null}

        {current.missing_terms?.length ? (
          <div className="mt-5">
            <div className="font-mono text-xs uppercase tracking-widest text-ink-faint">
              Wants, and your résumé does not show
            </div>
            <div className="mt-2 flex flex-wrap gap-2">
              {current.missing_terms.slice(0, 10).map((term) => (
                <span
                  key={term}
                  className="rounded-full border border-attn/40 px-3 py-1 text-xs text-attn"
                >
                  {term}
                </span>
              ))}
            </div>
          </div>
        ) : null}

        <a
          href={current.url}
          target="jobrunner-form"
          className="mt-7 inline-block font-mono text-xs text-ink-soft underline decoration-rule underline-offset-4 hover:text-ink"
        >
          read the posting ↗
        </a>
      </article>

      {error ? <p className="font-mono text-xs text-stop">{error}</p> : null}

      <div className="flex items-center gap-4">
        <button
          type="button"
          onClick={() => void decide("skipped")}
          disabled={pending !== null}
          className="flex-1 rounded-[var(--radius)] border border-rule px-6 py-4 font-mono text-sm text-ink-soft transition-colors hover:border-stop hover:text-stop disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-stop"
        >
          ← skip
        </button>
        <button
          type="button"
          onClick={() => void decide("interested")}
          disabled={pending !== null}
          className="flex-1 rounded-[var(--radius)] border border-go bg-go/10 px-6 py-4 font-mono text-sm text-go transition-colors hover:bg-go hover:text-on-accent disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-go"
        >
          worth applying to →
        </button>
      </div>

      <div className="flex justify-between font-mono text-xs text-ink-faint">
        <span>← → to decide</span>
        <span>
          {decided.interested} kept · {decided.skipped} skipped · {queue.length} left
        </span>
      </div>
    </div>
  );
}
