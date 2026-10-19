import { ApiError, api, type JudgmentCandidate, type JudgmentSummary } from "@/lib/api";
import { ErrorPanel } from "@/components/error-panel";
import { JudgmentDeck } from "@/components/judgment-deck";
import { GradeTabs } from "../tabs";

export const dynamic = "force-dynamic";

const BATCH_SIZE = 20;

/**
 * Grade the assistant's search: is this posting what the question asked?
 *
 * Phase 5 of docs/HARNESS_AUDIT_2026-10-15.md. The questions are frozen
 * (seeds/chat_judgment_questions.json), and the postings are what each search
 * mode returned for them, pooled by `make judge-pool-write`. These grades are
 * the baseline every change to the search is measured against, so the dev
 * questions are served first and the held-out ones after: work on the search
 * can start on the first half without reading the second.
 */
export default async function JudgeQuestionsPage() {
  let queue: JudgmentCandidate[];
  let summary: JudgmentSummary;
  try {
    [queue, summary] = await Promise.all([
      api.judgmentQueue(BATCH_SIZE),
      api.judgmentSummary(),
    ]);
  } catch (error) {
    if (error instanceof ApiError) return <ErrorPanel error={error} />;
    throw error;
  }

  const progress = summary.total ? Math.round((summary.graded / summary.total) * 100) : 0;

  return (
    <div className="mx-auto max-w-2xl space-y-8">
      <header className="space-y-3">
        <h1 className="font-display text-[length:var(--text-display)] leading-none">Grade</h1>
        <GradeTabs active="questions" />
        <p className="text-sm text-ink-soft">
          Each card is one posting the assistant&apos;s search returned for the question above it.
          Grade whether it is what the question asked for. Nothing is applied to, and which
          search found it is hidden on purpose.
        </p>
      </header>

      <JudgmentDeck initial={queue} />

      <section
        aria-labelledby="judged"
        className="rounded-[var(--radius-lg)] border border-rule-soft bg-paper-raised/50 p-6"
      >
        <h2 id="judged" className="font-mono text-xs uppercase tracking-widest text-ink-faint">
          The test set so far{summary.set_version ? ` · ${summary.set_version}` : ""}
        </h2>

        <div
          className="mt-4 h-1.5 overflow-hidden rounded-full bg-rule-soft"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={progress}
          aria-label="Graded"
        >
          <div className="h-full bg-go" style={{ width: `${progress}%` }} />
        </div>

        <dl className="mt-4 grid gap-x-8 gap-y-2 sm:grid-cols-[auto_1fr]">
          <dt className="font-mono text-xs text-ink-faint">Graded</dt>
          <dd className="text-sm">
            {summary.graded} of {summary.total}
            <span className="ml-2 text-ink-faint">({progress}%)</span>
          </dd>

          <dt className="font-mono text-xs text-ink-faint">Questions done</dt>
          <dd className="text-sm">
            {summary.questions_done} of {summary.questions}
          </dd>

          <dt className="font-mono text-xs text-ink-faint">Dev / held out</dt>
          <dd className="font-mono text-sm">
            {summary.dev.graded}/{summary.dev.total} · {summary.held_out.graded}/
            {summary.held_out.total}
          </dd>

          <dt className="font-mono text-xs text-ink-faint">By grade</dt>
          <dd className="font-mono text-sm">
            {[0, 1, 2].map((g) => `${g}:${summary.by_grade[String(g)] ?? 0}`).join("  ")}
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
      </section>
    </div>
  );
}
