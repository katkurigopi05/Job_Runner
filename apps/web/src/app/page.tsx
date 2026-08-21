import Link from "next/link";
import {
  ApiError,
  NEEDS_OWNER,
  api,
  type Application,
  type ApplicationStatus,
  type Calibration,
  type Digest,
  type Match,
  type MatchSummary,
} from "@/lib/api";
import { StatusPill } from "@/components/status";
import { ErrorPanel } from "@/components/error-panel";

export const dynamic = "force-dynamic";

/**
 * One screen that answers "what is the state of this, and what do I do next".
 *
 * The dashboard had grown ten pages, and reading it meant visiting most of
 * them: counts here, the queue there, the score threshold somewhere else. At
 * five applications a day that is a mild annoyance. At the volume this is
 * being pointed at, navigation *is* the cost.
 *
 * So every panel below is a summary with a link, never a second copy of
 * another page. The rule is that this screen tells you whether you need to go
 * somewhere, and the other pages are where the work happens.
 */

const ORDER: ApplicationStatus[] = [
  "needs_review",
  "needs_otp",
  "running",
  "queued",
  "submitted",
  "failed",
];

/** A count with a label. No chart — six numbers do not need one. */
function Stat({ value, label, href }: { value: string | number; label: string; href?: string }) {
  const body = (
    <>
      <div className="font-display text-3xl leading-none tabular-nums">{value}</div>
      <div className="mt-2 font-mono text-xs uppercase tracking-widest text-ink-faint">{label}</div>
    </>
  );
  return href ? (
    <Link
      href={href}
      className="rounded-[var(--radius)] border border-rule-soft bg-paper-raised/60 p-5 transition-colors hover:border-go"
    >
      {body}
    </Link>
  ) : (
    <div className="rounded-[var(--radius)] border border-rule-soft bg-paper-raised/60 p-5">
      {body}
    </div>
  );
}

function Panel({
  title,
  href,
  linkLabel,
  children,
}: {
  title: string;
  href?: string;
  linkLabel?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-[var(--radius-lg)] border border-rule bg-paper-raised p-6">
      <div className="flex items-baseline justify-between gap-4">
        <h2 className="font-mono text-xs uppercase tracking-widest text-ink-faint">{title}</h2>
        {href ? (
          <Link href={href} className="font-mono text-xs text-ink-faint hover:text-go">
            {linkLabel ?? "open"} →
          </Link>
        ) : null}
      </div>
      <div className="mt-4">{children}</div>
    </section>
  );
}

export default async function DeskPage() {
  let applications: Application[];
  let matches: Match[];
  let digest: Digest;
  let calibration: Calibration;
  let matchCounts: MatchSummary;

  try {
    // One round of parallel reads rather than one per panel. Everything here
    // is a summary endpoint, so the whole screen costs four requests.
    [applications, matches, digest, calibration, matchCounts] = await Promise.all([
      api.applications(),
      api.matches(false),
      api.digest(),
      api.calibration(),
      api.matchSummary(),
    ]);
  } catch (error) {
    if (error instanceof ApiError) return <ErrorPanel error={error} />;
    throw error;
  }

  const counts = new Map<ApplicationStatus, number>();
  for (const application of applications) {
    counts.set(application.status, (counts.get(application.status) ?? 0) + 1);
  }
  const waiting = applications.filter((a) => NEEDS_OWNER.includes(a.status));
  const recent = [...applications]
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    .slice(0, 5);
  const topMatches = matches.slice(0, 6);

  return (
    <div className="space-y-10">
      <header className="space-y-3">
        <h1 className="font-display text-[length:var(--text-display)] leading-[1.05] tracking-tight">
          {waiting.length > 0 ? `${waiting.length} waiting on you` : "Nothing waiting on you"}
        </h1>
        <p className="text-sm text-ink-soft">
          {waiting.length > 0 ? (
            <Link href="/finish" className="text-go underline decoration-rule underline-offset-4">
              Work the queue →
            </Link>
          ) : (
            "The queue is clear."
          )}
        </p>
      </header>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat value={digest.postings_seen.toLocaleString()} label="postings, 7 days" />
        <Stat value={matchCounts.total.toLocaleString()} label="matches" href="/matches" />
        <Stat value={matchCounts.undecided.toLocaleString()} label="unrated" href="/swipe" />
        <Stat value={waiting.length} label="waiting on you" href="/finish" />
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        {/* The score threshold is the project's live defect, so it gets a
            panel rather than a line on another page. */}
        <Panel title="Score calibration" href="/swipe" linkLabel="rate">
          {calibration.enough_data ? (
            <dl className="space-y-2 text-sm">
              <div className="flex justify-between gap-4">
                <dt className="text-ink-faint">Suggested min score</dt>
                <dd className="text-go tabular-nums">{calibration.suggested_min_score}</dd>
              </div>
              <div className="flex justify-between gap-4">
                <dt className="text-ink-faint">Kept vs skipped</dt>
                <dd className="tabular-nums">
                  {calibration.interested_mean} vs {calibration.skipped_mean}
                </dd>
              </div>
            </dl>
          ) : (
            <p className="text-sm text-ink-soft">
              {calibration.decided} rated. Ten kept postings are needed before a threshold can be
              derived — fewer would be noise dressed as a measurement.
            </p>
          )}
        </Panel>

        <Panel title="This week" href="/tracker" linkLabel="tracker">
          {digest.quiet_week ? (
            <p className="text-sm text-attn">
              Nothing in, nothing out. A quiet week usually means the crawler stopped, not that the
              market did.
            </p>
          ) : (
            <dl className="space-y-2 text-sm">
              {[
                ["Applications created", digest.applications_created],
                ["Submitted", digest.applications_submitted],
                ["Replies", digest.replies_received],
                ["Follow-ups due", digest.follow_ups_due],
              ].map(([label, value]) => (
                <div key={String(label)} className="flex justify-between gap-4">
                  <dt className="text-ink-faint">{label}</dt>
                  <dd className="tabular-nums">{value}</dd>
                </div>
              ))}
            </dl>
          )}
        </Panel>
      </div>

      <Panel title="Top matches" href="/matches" linkLabel="all">
        {topMatches.length ? (
          <ul className="divide-y divide-rule-soft">
            {topMatches.map((match) => (
              <li key={match.id} className="flex items-baseline justify-between gap-4 py-2.5">
                <span className="min-w-0 truncate text-sm">{match.title ?? match.url}</span>
                <span className="shrink-0 font-mono text-xs text-ink-faint tabular-nums">
                  {match.score.toFixed(3)}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-ink-soft">
            No matches yet. Run a crawl, then score against your profile.
          </p>
        )}
      </Panel>

      <Panel title="Pipeline" href="/applications" linkLabel="all">
        <div className="flex flex-wrap gap-x-6 gap-y-2 font-mono text-xs">
          {ORDER.map((status) => (
            <span key={status} className="flex items-center gap-2">
              <StatusPill status={status} reason={null} />
              <span className="tabular-nums text-ink-faint">{counts.get(status) ?? 0}</span>
            </span>
          ))}
        </div>
        {recent.length ? (
          <ul className="mt-5 divide-y divide-rule-soft">
            {recent.map((application) => (
              <li key={application.id} className="flex items-baseline justify-between gap-4 py-2.5">
                <Link
                  href={`/applications/${application.id}`}
                  className="min-w-0 truncate text-sm hover:text-go"
                >
                  {application.url}
                </Link>
                <StatusPill status={application.status} reason={application.failure_reason} />
              </li>
            ))}
          </ul>
        ) : null}
      </Panel>
    </div>
  );
}
