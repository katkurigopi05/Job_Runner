"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { type JudgmentCandidate } from "@/lib/api";
import { recordJudgment } from "@/app/label/questions/actions";

/**
 * Grade what the assistant's search returned, one posting against one question.
 *
 * Phase 5 of docs/HARNESS_AUDIT_2026-10-15.md: before any change to how the
 * search ranks, the owner says which results are what was asked. The postings
 * are every search mode's answers pooled together, in a stable shuffle, and
 * the card never says which mode found one: a grade given knowing whose answer
 * it is leans toward that mode.
 *
 * The scale is about the question, not about wanting the job. A posting can
 * be exactly what was asked and a job the owner would never take.
 */

const GRADES = [
  { value: 0, key: "0", label: "not it", hint: "not what was asked", tone: "stop" },
  { value: 1, key: "1", label: "partly", hint: "near it, or half of what was asked", tone: "soft" },
  { value: 2, key: "2", label: "what I asked for", hint: "an answer to the question", tone: "go" },
] as const;

/** Words that frame a question rather than name what it is about. */
const FRAMING = new Set(
  (
    "any all and are at for from in into jobs job role roles position positions posting " +
    "postings open the that this those with which what show me find using use uses need " +
    "needs needed mention mentioned on of or to who work working there"
  ).split(" "),
);

function questionWords(question: string): RegExp | null {
  const words = Array.from(
    new Set(
      question
        .toLowerCase()
        .split(/[^a-z0-9+#.]+/)
        .map((word) => word.replace(/\.+$/, ""))
        .filter((word) => word.length >= 3 && !FRAMING.has(word)),
    ),
  );
  if (words.length === 0) return null;
  const escaped = words.map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  return new RegExp(`(${escaped.join("|")})`, "gi");
}

/** The posting's own text, with the question's words marked. Text nodes only. */
function Marked({ text, pattern }: { text: string; pattern: RegExp | null }) {
  if (!pattern) return <>{text}</>;
  return (
    <>
      {text.split(pattern).map((part, index) =>
        index % 2 === 1 ? (
          <mark key={index} className="rounded-sm bg-attn/25 px-0.5 text-ink">
            {part}
          </mark>
        ) : (
          part
        ),
      )}
    </>
  );
}

export function JudgmentDeck({
  initial,
  remaining,
}: {
  initial: JudgmentCandidate[];
  /** Ungraded in the whole set when the page was rendered, this batch included. */
  remaining: number;
}) {
  const router = useRouter();
  const [queue, setQueue] = useState(initial);
  const [pending, setPending] = useState<number | null>(null);
  const [graded, setGraded] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const current = queue[0];
  const pattern = useMemo(() => (current ? questionWords(current.question) : null), [current]);

  const grade = useCallback(
    async (relevance: number) => {
      if (!current || pending !== null) return;
      setPending(relevance);
      setError(null);
      try {
        const result = await recordJudgment(current.id, relevance);
        if (!result.ok) {
          setError(result.message);
          return;
        }
        setGraded((n) => n + 1);
        // Only once the write succeeded, as on /label: a card dropped first
        // is a grade lost whenever the API is down.
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
      // A held key repeats, and the repeat would grade the next card unseen.
      if (event.repeat) return;
      const target = event.target as HTMLElement | null;
      if (target && ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return;
      const match = GRADES.find((g) => g.key === event.key);
      if (match) void grade(match.value);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [grade]);

  if (!current) {
    // A batch is twenty. Saying only "reload for the next batch" left ten
    // ungraded on 2026-10-19 because the screen read as finished.
    const waiting = Math.max(0, remaining - graded);
    return (
      <div className="rounded-[var(--radius-lg)] border border-rule bg-paper-raised p-10 text-center">
        <p className="font-display text-2xl">
          {waiting > 0 ? `${waiting} more waiting` : "Everything is graded"}
        </p>
        <p className="mt-3 text-sm text-ink-soft">
          {waiting > 0
            ? `${graded} graded in this batch. The next batch is ready.`
            : graded > 0
              ? `${graded} graded in this batch. Freeze the baseline with make score-judgments freeze=1.`
              : "Nothing is waiting. If the summary below is empty, the pool has not been built."}
        </p>
        {waiting > 0 ? (
          <button
            type="button"
            onClick={() => router.refresh()}
            className="mt-6 rounded bg-ink px-4 py-1.5 font-mono text-xs uppercase tracking-widest text-paper transition-opacity hover:opacity-85 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn"
          >
            Load the next batch
          </button>
        ) : null}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <section
        aria-label="The question"
        className="rounded-[var(--radius-lg)] border border-ink/70 bg-surface px-6 py-5"
      >
        <div className="flex items-baseline justify-between gap-4 font-mono text-xs text-ink-faint">
          <span className="uppercase tracking-widest">asked</span>
          <span>
            posting {current.position} of {current.of} for this question
          </span>
        </div>
        <p className="mt-2 font-display text-[1.6rem] leading-snug">“{current.question}”</p>
      </section>

      <article className="rounded-[var(--radius-lg)] border border-rule bg-paper-raised p-8 shadow-[var(--shadow-panel)]">
        <h2 className="font-display text-2xl leading-tight">{current.title ?? "Untitled role"}</h2>
        <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 font-mono text-xs text-ink-faint">
          {current.company ? <span>{current.company}</span> : null}
          {current.location ? <span>{current.location}</span> : null}
          {current.closed ? <span className="text-attn">closed since it was pooled</span> : null}
          {current.changed ? (
            <span className="text-attn">text changed since it was pooled</span>
          ) : null}
        </div>

        {current.description ? (
          <p className="mt-6 max-h-80 overflow-y-auto whitespace-pre-wrap text-sm leading-relaxed text-ink-soft">
            <Marked text={current.description} pattern={pattern} />
          </p>
        ) : (
          <p className="mt-6 text-sm text-ink-faint">This posting has no description stored.</p>
        )}

        {/* The URL is from a crawled third-party page: `noopener` keeps it
            from reaching back through `window.opener` to the dashboard. */}
        <a
          href={current.url}
          target="jobrunner-form"
          rel="noopener noreferrer"
          className="mt-7 inline-block font-mono text-xs text-ink-soft underline decoration-rule underline-offset-4 hover:text-ink"
        >
          read the posting ↗
        </a>
      </article>

      {error ? <p className="font-mono text-xs text-stop">{error}</p> : null}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
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
        <span>0–2 to grade · grade the question, not whether you want the job</span>
        <span>
          {graded} graded · {queue.length} left in this batch
        </span>
      </div>
    </div>
  );
}
