"use client";

import { useCallback, useEffect, useState } from "react";
import { type LabelCandidate } from "@/lib/api";
import { recordGrade } from "@/app/label/actions";

/**
 * Grade one posting at a time on the 0–3 relevance scale.
 *
 * This is not `/swipe` with more buttons, and the difference is the point.
 * `packages/matching/feedback.py` records two weaknesses in swipe-derived
 * labels: a swipe is binary, and it is taken in feed order, so only postings
 * the ranker already surfaced are ever judged. A four-point scale on the same
 * feed would fix the first and leave the second — while stamping the result
 * `provenance: owner`, the grade a benchmark trusts most.
 *
 * So the queue is drawn across three streams, and the badge on each card says
 * which one served it. `unseen` means the ranker never scored this posting at
 * all; those are the only grades that can measure what it buried.
 *
 * **A grade applies to nothing**, exactly as a swipe does not. §2.3 keeps
 * submission behind explicit approval.
 */

const GRADES = [
  { value: 0, key: "0", label: "irrelevant", hint: "would not apply", tone: "stop" },
  { value: 1, key: "1", label: "plausible", hint: "would read it", tone: "soft" },
  { value: 2, key: "2", label: "good", hint: "would apply", tone: "go" },
  { value: 3, key: "3", label: "excellent", hint: "would apply today", tone: "go" },
] as const;

const STREAM_COPY: Record<LabelCandidate["stream"], string> = {
  uncertain: "the ranker is unsure about this one",
  unseen: "the ranker never scored this — it cannot be swiped",
  confident: "one of the ranker's top picks",
};

export function LabelDeck({ initial }: { initial: LabelCandidate[] }) {
  const [queue, setQueue] = useState(initial);
  const [pending, setPending] = useState<number | null>(null);
  const [graded, setGraded] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const current = queue[0];

  const grade = useCallback(
    async (relevance: number) => {
      if (!current || pending !== null) return;
      setPending(relevance);
      setError(null);
      try {
        const result = await recordGrade(current.posting_id, relevance);
        if (!result.ok) {
          setError(result.message);
          return;
        }
        setGraded((n) => n + 1);
        // Drop the card only once the write succeeded. Removing it first
        // would lose the grade silently whenever the API is down, and the
        // whole point is that the grades accumulate.
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
      const match = GRADES.find((g) => g.key === event.key);
      if (match) void grade(match.value);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [grade]);

  if (!current) {
    return (
      <div className="rounded-[var(--radius-lg)] border border-rule bg-paper-raised p-10 text-center">
        <p className="font-display text-2xl">Nothing left to grade</p>
        <p className="mt-3 text-sm text-ink-soft">
          {graded > 0
            ? `${graded} graded this session. Reload for the next batch.`
            : "Run a crawl first — there are no postings to grade."}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <article className="rounded-[var(--radius-lg)] border border-rule bg-paper-raised p-8 shadow-[var(--shadow-panel)]">
        <div className="flex items-start justify-between gap-6">
          <h2 className="font-display text-2xl leading-tight">
            {current.title ?? "Untitled role"}
          </h2>
          <span className="shrink-0 font-mono text-sm text-ink-faint">
            {/* null is not zero: it means the ranker never weighed in, which
                is the fact the unseen stream exists to capture. */}
            {current.score === null ? "unscored" : current.score.toFixed(3)}
          </span>
        </div>

        <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 font-mono text-xs text-ink-faint">
          {current.location ? <span>{current.location}</span> : null}
        </div>

        <div className="mt-5 rounded-[var(--radius)] border border-rule px-4 py-3">
          <div className="font-mono text-xs uppercase tracking-widest text-ink-faint">
            {current.stream}
          </div>
          <p className="mt-1 text-sm text-ink-soft">{STREAM_COPY[current.stream]}</p>
        </div>

        {current.description ? (
          <p className="mt-6 max-h-64 overflow-y-auto whitespace-pre-wrap text-sm leading-relaxed text-ink-soft">
            {current.description}
          </p>
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

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {GRADES.map((g) => (
          <button
            key={g.value}
            type="button"
            onClick={() => void grade(g.value)}
            disabled={pending !== null}
            className={`rounded-[var(--radius)] border px-4 py-4 text-left transition-colors disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 ${
              g.tone === "go"
                ? "border-go/50 hover:bg-go hover:text-on-accent focus-visible:outline-go"
                : g.tone === "stop"
                  ? "border-rule hover:border-stop hover:text-stop focus-visible:outline-stop"
                  : "border-rule hover:border-ink focus-visible:outline-ink"
            }`}
          >
            <div className="font-mono text-xs text-ink-faint">{g.key}</div>
            <div className="mt-1 font-mono text-sm">{g.label}</div>
            <div className="mt-1 text-xs text-ink-faint">{g.hint}</div>
          </button>
        ))}
      </div>

      <div className="flex justify-between font-mono text-xs text-ink-faint">
        <span>0–3 to grade</span>
        <span>
          {graded} graded · {queue.length} left in this batch
        </span>
      </div>
    </div>
  );
}
