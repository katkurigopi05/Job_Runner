import Link from "next/link";
import { ApiError, api, type Calibration, type Match } from "@/lib/api";
import { ErrorPanel } from "@/components/error-panel";
import { SwipeDeck } from "@/components/swipe-deck";

export const dynamic = "force-dynamic";

const DECK_SIZE = 40;

/**
 * Rate postings one at a time.
 *
 * Two reasons this exists, and the second is the one that matters.
 *
 * **A ranked list of 1,853 postings is not something anyone reads.** One card
 * at a time is.
 *
 * **It is the only source of labelled data this project has.** CLAUDE.md §15
 * says Gate 5's "hand-labeled set of 20 postings — the ones you'd actually
 * apply to" does not exist, and that the fixtures standing in for it do not
 * answer the question the gate was written to ask. Every swipe is one real
 * label, produced by using the tool. `/matches/calibration` reads them back
 * and derives a threshold from them — which is what `min_match_score` has
 * needed since the first real scoring run returned a maximum of 0.271 against
 * a default of 0.75.
 */
export default async function SwipePage() {
  let matches: Match[];
  let calibration: Calibration;
  try {
    const query = new URLSearchParams({
      undecided_only: "true",
      include_applied: "false",
      limit: String(DECK_SIZE),
    });
    [matches, calibration] = await Promise.all([
      api.matchesFiltered(query),
      api.calibration(),
    ]);
  } catch (error) {
    if (error instanceof ApiError) return <ErrorPanel error={error} />;
    throw error;
  }

  return (
    <div className="mx-auto max-w-2xl space-y-8">
      <header className="space-y-3">
        <h1 className="font-display text-[length:var(--text-display)] leading-none">Rate</h1>
        <p className="text-sm text-ink-soft">
          Keep or skip. Nothing is applied to — this records what you think of the posting, and
          the application is still started deliberately from the{" "}
          <Link href="/matches" className="underline decoration-rule underline-offset-4">
            match feed
          </Link>
          .
        </p>
      </header>

      <SwipeDeck initial={matches} />

      {/* The payoff, shown rather than hidden in an endpoint: what these
          decisions say the score threshold should be. */}
      <section
        aria-labelledby="calibration"
        className="rounded-[var(--radius-lg)] border border-rule-soft bg-paper-raised/50 p-6"
      >
        <h2 id="calibration" className="font-mono text-xs uppercase tracking-widest text-ink-faint">
          What your ratings say about the score
        </h2>
        {calibration.enough_data ? (
          <dl className="mt-4 grid gap-x-8 gap-y-2 sm:grid-cols-[auto_1fr]">
            <dt className="font-mono text-xs text-ink-faint">Suggested min score</dt>
            <dd className="text-sm text-go">{calibration.suggested_min_score}</dd>
            <dt className="font-mono text-xs text-ink-faint">Kept vs skipped</dt>
            <dd className="text-sm">
              {calibration.interested_mean} vs {calibration.skipped_mean}
              {calibration.separation !== null && calibration.separation <= 0 ? (
                <span className="ml-2 text-attn">
                  — the scorer is not ranking what you want; no threshold fixes that
                </span>
              ) : null}
            </dd>
          </dl>
        ) : (
          <p className="mt-3 text-sm text-ink-soft">
            {calibration.decided} rated so far. A suggested threshold needs at least 10 kept
            postings — a number derived from fewer would be noise dressed as a measurement.
          </p>
        )}
      </section>
    </div>
  );
}
