import Link from "next/link";
import { ApiError, api, type Match, type Profile } from "@/lib/api";
import { ErrorPanel } from "@/components/error-panel";
import { FilterBar } from "./filter-bar";
import { Suspense } from "react";

export const dynamic = "force-dynamic";

/** Where a score sits relative to the profile's own threshold. */
function verdict(score: number, threshold: number) {
  if (score >= threshold) return { label: "above your threshold", tone: "text-go" };
  if (score >= threshold * 0.75) return { label: "near your threshold", tone: "text-attn" };
  return { label: "below your threshold", tone: "text-ink-faint" };
}

const FILTER_KEYS = [
  "keywords",
  "locations",
  "remote",
  "min_seniority",
  "max_seniority",
  "posted_within_days",
] as const;

export default async function MatchesPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  let matches: Match[];
  let profiles: Profile[];
  try {
    const query = new URLSearchParams({ include_applied: "false" });
    for (const key of FILTER_KEYS) {
      const value = params[key];
      if (typeof value === "string" && value) query.set(key, value);
    }
    [matches, profiles] = await Promise.all([api.matchesFiltered(query), api.profiles()]);
  } catch (error) {
    if (error instanceof ApiError) return <ErrorPanel error={error} />;
    throw error;
  }

  const thresholdFor = (profileId: string) =>
    profiles.find((profile) => profile.id === profileId)?.min_match_score ?? 0.75;

  return (
    <div className="space-y-10">
      <header>
        <h1 className="font-display text-display leading-[1.05] tracking-tight">Matches</h1>
        <p className="mt-3 max-w-prose text-ink-soft">
          Postings the crawler found, scored against your profile. Already-applied ones are hidden.
          Each score carries the reasoning behind it — this number decides what gets applied to, so
          it should be arguable rather than taken on trust.
        </p>
      </header>

      <Suspense fallback={null}>
        <FilterBar resultCount={matches.length} />
      </Suspense>

      {matches.length === 0 ? (
        <div className="rounded-[var(--radius-lg)] border border-dashed border-rule px-6 py-16 text-center">
          <p className="font-mono text-sm text-ink-faint">nothing scored yet</p>
          <p className="mx-auto mt-3 max-w-prose text-sm text-ink-soft">
            Scoring happens during a crawl. Run the worker and give it a cycle over the company
            registry in <code className="font-mono text-xs">seeds/companies.yaml</code>.
          </p>
        </div>
      ) : (
        <ul className="space-y-4">
          {matches.map((match) => {
            const threshold = thresholdFor(match.profile_id);
            const call = verdict(match.score, threshold);
            const percent = Math.round(match.score * 100);
            return (
              <li
                key={match.id}
                className="rounded-[var(--radius-lg)] border border-rule bg-paper-raised p-5 shadow-[var(--shadow-soft)]"
              >
                <div className="flex flex-wrap items-baseline justify-between gap-4">
                  <h2 className="font-display text-lg">{match.title ?? "Untitled posting"}</h2>
                  <div className="text-right">
                    <p className={`font-mono text-2xl tabular-nums ${call.tone}`}>{percent}</p>
                    <p className="font-mono text-xs text-ink-faint">{call.label}</p>
                  </div>
                </div>

                <p className="mt-1 font-mono text-xs text-ink-faint">
                  {match.location ?? "location not stated"} · {match.ats_type ?? "unknown ats"}
                  {match.closed ? " · closed" : ""}
                </p>

                {/* The breakdown. Two similarities and any hard filter that
                    ruled it out — the parts the score is made of. */}
                <dl className="mt-4 flex flex-wrap gap-x-8 gap-y-2 font-mono text-xs text-ink-soft">
                  <div className="flex gap-2">
                    <dt className="text-ink-faint">title</dt>
                    <dd className="tabular-nums">{Math.round(match.title_similarity * 100)}%</dd>
                  </div>
                  <div className="flex gap-2">
                    <dt className="text-ink-faint">body</dt>
                    <dd className="tabular-nums">{Math.round(match.body_similarity * 100)}%</dd>
                  </div>
                </dl>

                {match.excluded_by.length > 0 ? (
                  <p className="mt-3 rounded-[var(--radius)] border border-stop/40 bg-stop-soft px-3 py-2 font-mono text-xs text-stop">
                    ruled out by {match.excluded_by.join(", ")} — a hard filter, not a low score
                  </p>
                ) : null}

                <div className="mt-4 flex flex-wrap items-center gap-4">
                  <a
                    href={match.url}
                    target="_blank"
                    rel="noreferrer"
                    className="font-mono text-xs text-ink-soft underline-offset-4 hover:text-ink hover:underline"
                  >
                    open posting ↗
                  </a>
                  <Link
                    href="/review"
                    className="font-mono text-xs text-ink-faint underline-offset-4 hover:text-ink-soft hover:underline"
                  >
                    review queue
                  </Link>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
