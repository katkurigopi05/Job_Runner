import Link from "next/link";
import {
  ApiError,
  api,
  type Legitimacy,
  type Match,
  type Profile,
  type Rubric,
} from "@/lib/api";
import { ErrorPanel } from "@/components/error-panel";
import { FilterBar } from "./filter-bar";
import { AtsPanel } from "./ats-panel";
import { DiscoveryStrip } from "./discovery-strip";
import { EligibilityNote } from "@/components/eligibility-note";
import { Suspense } from "react";

export const dynamic = "force-dynamic";

/** Where a score sits relative to the profile's own threshold. */
/**
 * Where a score sits relative to the profile's own threshold.
 *
 * The **words lead and the number follows**, which is a reversal, and the
 * reason is that the number is not a grade. It is a cosine similarity, and
 * CLAUDE.md §15 records the measurement that settles it: the shipped
 * threshold of 0.75 was unreachable, because the first real run over 10,922
 * postings peaked at **0.271**. So a good match renders as "27%" and a
 * typical one as "8%", and a feed of single-digit percentages reads as "you
 * are a poor fit for everything" when it means nothing of the sort.
 *
 * `legitimacy.py` already made this call for the other score in this app —
 * "a tier and a findings table, never a number", because a number invites a
 * precision the thing does not have. Same argument, same answer.
 *
 * **The label is rank within this feed, not distance from the threshold**,
 * and the first draft of this got that wrong. Measured on the owner's own
 * data: 74 matches spanning 0.000–0.081, against profile thresholds of 0.75
 * and 0.0. Threshold-relative labels therefore collapse — every posting is a
 * "long shot" under one profile and a "strong match" under the other, and
 * neither tells you anything. Rank is self-calibrating: it always spreads,
 * it cannot degenerate, and "near the top of what you are being shown" stays
 * true whatever the absolute scale turns out to be.
 *
 * Clearing the owner's own threshold is still worth saying, so it is said
 * separately rather than folded in here.
 */
function verdict(rank: number, total: number) {
  const position = total <= 1 ? 0 : rank / (total - 1);
  if (position <= 0.1) return { label: "Best of these", tone: "text-go" };
  if (position <= 0.35) return { label: "Strong", tone: "text-go" };
  if (position <= 0.7) return { label: "Worth a look", tone: "text-ink-soft" };
  return { label: "Further down", tone: "text-ink-faint" };
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
          What the crawler found, scored against your profile. Every score shows its working, so
          you can disagree with it.
        </p>
      </header>

      {/* Before the feed, because what the feed is *missing* is the question
          an empty one raises — and a crawl part-way through thousands of
          companies reads identically to a broken pipeline without it. */}
      <Suspense fallback={null}>
        <DiscoveryStrip />
      </Suspense>

      <Suspense fallback={null}>
        <FilterBar resultCount={matches.length} />
      </Suspense>

      {matches.length === 0 ? (
        <div className="rounded-[var(--radius-lg)] border border-dashed border-rule px-6 py-16 text-center">
          <p className="text-sm text-ink-faint">nothing scored yet</p>
          <p className="mx-auto mt-3 max-w-prose text-sm text-ink-soft">
            Scoring happens during a crawl. Run the worker and give it a cycle over the company
            registry in <code className="font-mono text-xs">seeds/companies.yaml</code>, or{" "}
            <code className="font-mono text-xs">make crawl dispatch=1</code> for companies imported
            from a spreadsheet. The counts above say which of those is still outstanding.
          </p>
        </div>
      ) : (
        <ul className="space-y-4">
          {matches.map((match, index) => {
            const threshold = thresholdFor(match.profile_id);
            const call = verdict(index, matches.length);
            const clears = match.score >= threshold;
            const percent = Math.round(match.score * 100);
            return (
              <li
                key={match.id}
                className="rounded-[var(--radius-lg)] border border-rule bg-paper-raised p-5 shadow-[var(--shadow-soft)]"
              >
                {/* Identity. The score anchors the card because it is what
                    the card is for — it was 24px and outweighed by its own
                    caption, while the caption sat wider than the number. */}
                <div className="flex items-start justify-between gap-6">
                  <div className="min-w-0">
                    <h2 className="font-display text-xl leading-tight">
                      {match.title ?? "Untitled posting"}
                    </h2>
                    <p className="mt-1.5 text-xs text-ink-soft">
                      {match.location ?? "location not stated"} ·{" "}
                      <span className="font-mono">{match.ats_type ?? "unknown ats"}</span>
                      {match.closed ? " · closed" : ""}
                      {/* How long we took to notice. The board not saying is
                          different from us being instant, so it reads
                          differently. */}
                      {match.lag_hours !== null ? (
                        <span className={match.lag_hours <= 24 ? "text-go" : ""}>
                          {" "}
                          · found{" "}
                          {match.lag_hours < 1
                            ? "within the hour"
                            : `${Math.round(match.lag_hours)}h`}{" "}
                          after posting
                        </span>
                      ) : (
                        " · no posting date"
                      )}
                    </p>
                  </div>
                  <div className="shrink-0 text-right">
                    <p className={`font-display text-lg leading-none ${call.tone}`}>{call.label}</p>
                    <p
                      className="mt-1.5 font-mono text-xs tabular-nums text-ink-faint"
                      title="Cosine similarity between this posting and your profile. It orders the feed; it is not a grade out of 100."
                    >
                      {percent}% similar
                    </p>
                    {clears ? (
                      <p className="mt-0.5 text-xs text-go">above your threshold</p>
                    ) : null}
                  </div>
                </div>

                {/* The breakdown. Two similarities and any hard filter that
                    ruled it out — the parts the score is made of. */}
                {/* Reasoning starts here — 24px down, where the previous
                    version put the same 16px it put between every other row. */}
                <dl className="mt-6 flex flex-wrap gap-x-8 gap-y-1 text-xs text-ink-soft">
                  <div className="flex gap-2">
                    <dt className="text-ink-faint">title match</dt>
                    <dd className="font-mono tabular-nums">
                      {Math.round(match.title_similarity * 100)}%
                    </dd>
                  </div>
                  <div className="flex gap-2">
                    <dt className="text-ink-faint">body match</dt>
                    <dd className="font-mono tabular-nums">
                      {Math.round(match.body_similarity * 100)}%
                    </dd>
                  </div>
                  {/* The weakest dimension stays on the face of the card even
                      with the breakdown collapsed: it is the one number that
                      says *why* the score is what it is. */}
                  {match.rubric?.weakest ? (
                    <div className="flex gap-2">
                      <dt className="text-ink-faint">held back by</dt>
                      <dd className="text-attn">{match.rubric.weakest.replace(/_/g, " ")}</dd>
                    </div>
                  ) : null}
                </dl>

                {match.matched_terms.length > 0 ? (
                  /* On the face of the card, in the positive colour. The API
                     has sent `matched_terms` all along and this screen never
                     rendered it — so the feed's answer to "how do I look for
                     this job" was a collapsed list of what you lack, and
                     nothing at all about what you have. */
                  <div className="mt-4">
                    <p className="text-xs text-ink-faint">What you already have for this</p>
                    <ul className="mt-2 flex flex-wrap gap-1.5">
                      {match.matched_terms.slice(0, 12).map((term) => (
                        <li
                          key={term}
                          className="rounded border border-go/40 bg-go-soft px-2 py-0.5 font-mono text-xs text-go"
                        >
                          {term}
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}

                {/* Collapsed, and the line above stays visible.
                    CLAUDE.md is right that a score you cannot argue with is
                    one you end up ignoring — but fifty cards at 600px each
                    made a 31,000px page, and reasoning nobody scrolls to is
                    not reasoning that travelled. So the *argument* (score,
                    both similarities, the weakest dimension, any restriction)
                    stays on the face of the card and the *evidence* is one
                    click away. */}
                {(match.rubric && match.rubric.dimensions.length > 0) ||
                match.missing_terms.length > 0 ? (
                  <details className="group mt-2">
                    <summary className="cursor-pointer list-none text-xs text-ink-faint underline-offset-4 hover:text-ink-soft hover:underline">
                      <span className="group-open:hidden">Show the breakdown ▸</span>
                      <span className="hidden group-open:inline">Hide the breakdown ▾</span>
                    </summary>

                    {match.rubric && match.rubric.dimensions.length > 0 ? (
                      <RubricBars rubric={match.rubric} />
                    ) : null}

                    {match.missing_terms.length > 0 ? (
                  /* Neutral, not amber. These are things to know, not things
                     to act on, and painting eight of them in the "needs you"
                     colour made the alarm the most common thing on the card. */
                  <div className="mt-6">
                    <p className="text-xs text-ink-faint">
                      Worth adding, if any of it is true of you
                    </p>
                    <ul className="mt-2 flex flex-wrap gap-1.5">
                      {match.missing_terms.map((term) => (
                        <li
                          key={term}
                          className="rounded border border-rule px-2 py-0.5 font-mono text-xs text-ink-soft"
                        >
                          {term}
                        </li>
                      ))}
                    </ul>
                    <p className="mt-2 max-w-prose text-xs text-ink-faint">
                      The tailorer works from what your résumé already supports, so these stay off
                      it until you add them.
                    </p>
                  </div>
                    ) : null}

                    <AtsPanel postingId={match.posting_id} profileId={match.profile_id} />
                  </details>
                ) : null}

                {/* Never collapsed. A ghost-job signal and a restriction the
                    owner may not clear are the two things that should stop
                    them before they spend an hour, so neither hides. */}
                {match.legitimacy && match.legitimacy.tier !== "high_confidence" ? (
                  <LegitimacyPanel legitimacy={match.legitimacy} />
                ) : null}

                <EligibilityNote eligibility={match.eligibility} />

                {match.excluded_by.length > 0 ? (
                  <p className="aside aside-stop mt-4 text-xs text-stop">
                    Ruled out by {match.excluded_by.join(", ")} — a hard filter, not a low score.
                  </p>
                ) : null}

                {/* Actions, set apart from the reasoning above them. */}
                <div className="mt-6 flex flex-wrap items-center gap-5 border-t border-rule-soft pt-4">
                  <a
                    href={match.url}
                    target="jobrunner-form"
                    rel="noreferrer"
                    className="text-xs text-ink-soft underline-offset-4 hover:text-ink hover:underline"
                  >
                    Open posting ↗
                  </a>
                  <Link
                    href="/review"
                    className="text-xs text-ink-faint underline-offset-4 hover:text-ink-soft hover:underline"
                  >
                    Review queue
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


/** The score, broken into the parts that produced it. */
function RubricBars({ rubric }: { rubric: Rubric }) {
  const scored = rubric.dimensions.filter((d) => d.weight > 0);
  if (scored.length === 0) return null;

  return (
    <div className="mt-3">
      {/* The scale was invisible: a 1.0 bar and a 5.0 bar were two grey slabs
          with no axis, so "1.0" and "5.0" read as unrelated numbers rather
          than as one fifth and full marks. */}
      <p className="text-xs text-ink-faint">Score out of 5, by dimension</p>
      <div className="mt-2 space-y-1.5">
        {scored.map((dimension) => {
          const isWeakest = dimension.name === rubric.weakest;
          return (
            <div key={dimension.name} className="flex items-center gap-3 text-xs">
              <span className="w-32 shrink-0 text-ink-faint">
                {dimension.name.replace(/_/g, " ")}
              </span>
              <span className="h-1.5 w-20 shrink-0 overflow-hidden rounded-full bg-rule-soft">
                <span
                  className={`block h-full rounded-full ${isWeakest ? "bg-attn" : "bg-ink-faint"}`}
                  style={{ width: `${(dimension.score / 5) * 100}%` }}
                />
              </span>
              <span className="w-10 shrink-0 font-mono tabular-nums text-ink-soft">
                {dimension.score.toFixed(1)}
              </span>
              <span className="min-w-0 text-ink-faint">{dimension.finding}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/**
 * Whether the posting looks real. Shown only when it is not
 * high_confidence — a warning on every card is one nobody reads.
 */
function LegitimacyPanel({ legitimacy }: { legitimacy: Legitimacy }) {
  const concerning = legitimacy.signals.filter((s) => s.weight === "concerning");
  const tone = legitimacy.tier === "suspicious" ? "aside-stop text-stop" : "aside-attn text-attn";

  return (
    <div className={`aside mt-4 ${tone}`}>
      <p className="text-xs font-medium">
        {legitimacy.tier === "suspicious"
          ? "Several ghost-job signals — worth checking before you spend time on it"
          : "Mixed signals on whether this posting is live"}
      </p>
      <ul className="mt-1 space-y-0.5">
        {concerning.map((signal) => (
          <li key={signal.name} className="text-xs opacity-80">
            {signal.name.replace(/_/g, " ")}: {signal.finding}
          </li>
        ))}
        {legitimacy.advisories.map((signal) => (
          <li key={signal.name} className="font-mono text-xs opacity-80">
            {signal.finding}
          </li>
        ))}
      </ul>
    </div>
  );
}
