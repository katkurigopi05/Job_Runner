import {
  ApiError,
  api,
  type RankingEvaluation,
  type RankingPreference,
  type RankingSuggestion,
} from "@/lib/api";
import { ErrorPanel } from "@/components/error-panel";
import { AcceptSuggestion, AddAdjustment, RemoveAdjustment } from "./controls";

export const dynamic = "force-dynamic";

/**
 * Your adjustments to the order, what your skips suggest, and whether any of
 * it has been shown to rank better.
 *
 * The base score is never changed. Adjustments produce a second number the
 * feed can order by when you ask it to, and every card says which adjustment
 * moved it. The evaluation panel is deliberately blunt: with no held-out
 * grades, nothing here has been validated, and it says so.
 */

function points(weight: number): string {
  return `${weight >= 0 ? "+" : "−"}${Math.round(Math.abs(weight) * 100)}`;
}

function Interval({ label, value, interval }: { label: string; value: number | null; interval: number[] | null }) {
  return (
    <div>
      <dt className="font-mono text-xs uppercase tracking-widest text-ink-faint">{label}</dt>
      <dd className="mt-1 font-display text-2xl tabular-nums">
        {value === null ? "—" : value.toFixed(3)}
        {interval ? (
          <span className="ml-2 font-mono text-xs text-ink-faint">
            [{interval[0].toFixed(3)}, {interval[1].toFixed(3)}]
          </span>
        ) : null}
      </dd>
    </div>
  );
}

function Evaluation({ report }: { report: RankingEvaluation | null }) {
  if (!report) {
    return <p className="text-sm text-ink-faint">Evaluation unavailable (needs exactly one profile).</p>;
  }
  const tone = report.promotable ? "text-go" : report.status === "evaluated" ? "text-ink-soft" : "text-attn";
  return (
    <div className="space-y-4">
      <p className={`max-w-prose text-sm ${tone}`}>{report.message}</p>
      <dl className="grid grid-cols-2 gap-6 sm:grid-cols-4">
        <div>
          <dt className="font-mono text-xs uppercase tracking-widest text-ink-faint">owner grades</dt>
          <dd className="mt-1 font-display text-2xl tabular-nums">{report.owner_labels}</dd>
        </div>
        <div>
          <dt className="font-mono text-xs uppercase tracking-widest text-ink-faint">held out</dt>
          <dd className="mt-1 font-display text-2xl tabular-nums">{report.held_out}</dd>
        </div>
        <Interval label={`base ndcg@${report.k}`} value={report.base_ndcg} interval={report.base_interval} />
        <Interval
          label={`personal ndcg@${report.k}`}
          value={report.personalized_ndcg}
          interval={report.personalized_interval}
        />
      </dl>
      <p className="text-xs text-ink-faint">Learned model: {report.learned_model}</p>
    </div>
  );
}

export default async function RankingPage() {
  let preferences: RankingPreference[];
  let suggestions: RankingSuggestion[];
  let evaluation: RankingEvaluation | null;
  try {
    [preferences, suggestions, evaluation] = await Promise.all([
      api.rankingPreferences(),
      api.rankingSuggestions(),
      api.rankingEvaluation().catch((error: unknown) => {
        if (error instanceof ApiError && error.code === "invalid_request") return null;
        throw error;
      }),
    ]);
  } catch (error) {
    if (error instanceof ApiError) return <ErrorPanel error={error} />;
    throw error;
  }

  return (
    <div className="space-y-12">
      <header>
        <h1 className="font-display text-display leading-[1.05] tracking-tight">Ranking</h1>
        <p className="mt-3 max-w-prose text-ink-soft">
          Nudge the order of your feed. Your fit score stays exactly as it is; choose “with my
          adjustments” on Matches to order by these, and each card shows what moved it.
        </p>
      </header>

      <section aria-labelledby="adjustments" className="space-y-4">
        <h2 id="adjustments" className="border-b border-rule pb-2 font-display text-2xl">
          Your adjustments
        </h2>
        {preferences.length === 0 ? (
          <p className="text-sm text-ink-faint">None yet.</p>
        ) : (
          <ul className="divide-y divide-rule-soft rounded-[var(--radius)] border border-rule bg-paper-raised">
            {preferences.map((preference) => (
              <li key={preference.id} className="flex flex-wrap items-baseline gap-x-4 gap-y-1 px-4 py-3">
                <span className={`w-10 font-mono text-sm tabular-nums ${preference.weight >= 0 ? "text-go" : "text-stop"}`}>
                  {points(preference.weight)}
                </span>
                <span className="font-mono text-xs text-ink-faint">{preference.kind.replace("_", " ")}</span>
                <span className="text-sm text-ink">{preference.value}</span>
                {preference.source === "suggestion" ? (
                  <span className="text-xs text-ink-faint">from your skips</span>
                ) : null}
                <span className="ml-auto">
                  <RemoveAdjustment id={preference.id} />
                </span>
              </li>
            ))}
          </ul>
        )}
        <AddAdjustment />
      </section>

      <section aria-labelledby="suggestions" className="space-y-4">
        <h2 id="suggestions" className="border-b border-rule pb-2 font-display text-2xl">
          Suggested by your skips
        </h2>
        {suggestions.length === 0 ? (
          <p className="max-w-prose text-sm text-ink-faint">
            Nothing yet. When you skip with a reason on Rate, patterns with at least three decisions
            behind them show up here — as suggestions you accept, never as changes made for you.
          </p>
        ) : (
          <ul className="space-y-2">
            {suggestions.map((suggestion) => (
              <li
                key={`${suggestion.kind}:${suggestion.value}`}
                className="flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-[var(--radius)] border border-rule-soft px-4 py-3"
              >
                <span className="font-mono text-sm tabular-nums text-ink">{points(suggestion.weight)}</span>
                <span className="text-sm text-ink">
                  {suggestion.kind.replace("_", " ")}: {suggestion.value}
                </span>
                <span className="text-xs text-ink-faint">{suggestion.evidence}</span>
                <span className="ml-auto">
                  <AcceptSuggestion suggestion={suggestion} />
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="evaluation" className="space-y-4">
        <h2 id="evaluation" className="border-b border-rule pb-2 font-display text-2xl">
          Is it better?
        </h2>
        <Evaluation report={evaluation} />
      </section>
    </div>
  );
}
