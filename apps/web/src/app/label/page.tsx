import Link from "next/link";
import { ApiError, api, type LabelCandidate, type LabelSummary } from "@/lib/api";
import { ErrorPanel } from "@/components/error-panel";
import { LabelDeck } from "@/components/label-deck";

export const dynamic = "force-dynamic";

const BATCH_SIZE = 10;

/**
 * Grade postings 0–3 — docs/BACKLOG.md P1.
 *
 * Every relevance label in this repo is a `FIXTURE`: a posting and a grade
 * written together, beside the code that reads them. `docs/ML_EVALUATION.md`
 * refuses to name a production ranking candidate because of it, and CLAUDE.md
 * §15 records that Gate 5 therefore does not answer the question it was
 * written to ask — whether the scorer works on *this owner's* material.
 *
 * `/swipe` already collects real judgements, and they are worth having, but
 * `packages/matching/feedback.py` is explicit about their two limits: a swipe
 * is binary, and it is taken in feed order so only postings the ranker already
 * surfaced are ever judged. This screen fixes both — a four-point scale, and a
 * queue drawn across three streams including postings that were never scored.
 *
 * Fixing only the first would be worse than fixing neither: the labels would
 * carry `provenance: owner`, the grade a benchmark trusts most, while keeping
 * the sampling bias invisible.
 */
export default async function LabelPage() {
  let queue: LabelCandidate[];
  let summary: LabelSummary;
  try {
    [queue, summary] = await Promise.all([api.labelQueue(BATCH_SIZE), api.labelSummary()]);
  } catch (error) {
    if (error instanceof ApiError) return <ErrorPanel error={error} />;
    throw error;
  }

  const progress = Math.min(100, Math.round((summary.total / summary.target) * 100));

  return (
    <div className="mx-auto max-w-2xl space-y-8">
      <header className="space-y-3">
        <h1 className="font-display text-[length:var(--text-display)] leading-none">Grade</h1>
        <p className="text-sm text-ink-soft">
          A graded label is not a swipe. Nothing is applied to — this records how well the
          posting matches, on the scale the benchmark reads. Faster verdicts belong on{" "}
          <Link href="/swipe" className="underline decoration-rule underline-offset-4">
            Rate
          </Link>
          .
        </p>
      </header>

      <LabelDeck initial={queue} />

      {/* The payoff, and the honesty check. A count alone cannot tell a usable
          corpus from a self-confirming one, so the stream mix is shown beside
          it — labels drawn only from the ranker's shortlist carry exactly the
          bias `provenance: owner` is supposed to have escaped. */}
      <section
        aria-labelledby="corpus"
        className="rounded-[var(--radius-lg)] border border-rule-soft bg-paper-raised/50 p-6"
      >
        <h2 id="corpus" className="font-mono text-xs uppercase tracking-widest text-ink-faint">
          The corpus so far
        </h2>

        <dl className="mt-4 grid gap-x-8 gap-y-2 sm:grid-cols-[auto_1fr]">
          <dt className="font-mono text-xs text-ink-faint">Graded</dt>
          <dd className="text-sm">
            {summary.total} of {summary.target}
            <span className="ml-2 text-ink-faint">({progress}%)</span>
          </dd>

          <dt className="font-mono text-xs text-ink-faint">By grade</dt>
          <dd className="font-mono text-sm">
            {[0, 1, 2, 3].map((g) => `${g}:${summary.by_grade[String(g)] ?? 0}`).join("  ")}
          </dd>

          <dt className="font-mono text-xs text-ink-faint">By stream</dt>
          <dd className="font-mono text-sm">
            {Object.keys(summary.by_stream).length
              ? Object.entries(summary.by_stream)
                  .map(([stream, count]) => `${stream}:${count}`)
                  .join("  ")
              : "—"}
          </dd>
        </dl>

        {summary.notes.length ? (
          <ul className="mt-4 space-y-2">
            {summary.notes.map((note) => (
              <li key={note} className="text-sm text-attn">
                {note}
              </li>
            ))}
          </ul>
        ) : null}

        {summary.usable ? (
          <p className="mt-4 font-mono text-xs text-go">
            Usable. Export with: make export-labels kind=owner
          </p>
        ) : null}
      </section>
    </div>
  );
}
