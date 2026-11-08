import Link from "next/link";
import {
  ApiError, api, type Application, type Calibration, type Digest,
  type Match, type MatchSummary,
} from "@/lib/api";
import { StatusPill, STATUS_ORDER } from "@/components/status";
import { ErrorPanel } from "@/components/error-panel";
import {
  LivePipeline, LiveWaitingCount, LiveWaitingHeadline, StatusStreamProvider, type StatusCounts,
} from "@/components/status-stream";

export const dynamic = "force-dynamic";

function Stat({ value, label, detail, href }: {
  value: React.ReactNode; label: string; detail: string; href?: string;
}) {
  const body = <>
    <div className="desk-stat-label">{label}</div>
    <div className="desk-stat-value">{value}</div>
    <div className="desk-stat-detail">{detail}</div>
  </>;
  return href ? <Link href={href} className="desk-stat">{body}</Link> : <div className="desk-stat">{body}</div>;
}

function Panel({ title, href, linkLabel, children }: {
  title: string; href?: string; linkLabel?: string; children: React.ReactNode;
}) {
  return (
    <section className="desk-card">
      <div className="desk-card-heading">
        <h2>{title}</h2>
        {href ? <Link href={href}>{linkLabel ?? "Open"}</Link> : null}
      </div>
      {children}
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
    [applications, matches, digest, calibration, matchCounts] = await Promise.all([
      api.applications(), api.matches(false), api.digest(), api.calibration(), api.matchSummary(),
    ]);
  } catch (error) {
    if (error instanceof ApiError) return <ErrorPanel error={error} />;
    throw error;
  }
  const counts = Object.fromEntries(STATUS_ORDER.map((status) => [status, 0])) as StatusCounts;
  for (const application of applications) counts[application.status] = (counts[application.status] ?? 0) + 1;
  const recent = [...applications].sort((a, b) => b.updated_at.localeCompare(a.updated_at)).slice(0, 5);
  const topMatches = matches.slice(0, 6);
  const date = new Intl.DateTimeFormat("en-GB", {
    weekday: "long", day: "numeric", month: "long", timeZone: "America/Los_Angeles",
  }).format(new Date());

  return (
    <StatusStreamProvider initial={counts}>
      <div className="desk">
        <header>
          <p className="desk-eyebrow">Desk, {date}</p>
          <LiveWaitingHeadline initial={counts} />
        </header>
        <div className="desk-stats">
          <Stat value={digest.applications_submitted.toLocaleString("en-US")} label="Sent" detail="last 7 days" href="/applications" />
          <Stat value={matchCounts.total.toLocaleString("en-US")} label="Matches" detail="scored against your profile" href="/matches" />
          <Stat value={digest.postings_seen.toLocaleString("en-US")} label="Postings" detail="seen in the last 7 days" />
          <Stat value={<LiveWaitingCount initial={counts} />} label="Waiting on you" detail="in review or awaiting a code" href="/finish" />
        </div>
        <div className="desk-grid">
          <Panel title="Top matches" href="/matches" linkLabel="All matches">
            {topMatches.length ? (
              <ol className="divide-y divide-rule-soft">
                {topMatches.map((match) => (
                  <li key={match.id} className="flex min-h-10 items-center justify-between gap-3 py-2">
                    <span className="min-w-0 truncate">{match.title ?? match.url}</span>
                    <span className="shrink-0 font-mono text-xs text-ink-soft tabular-nums">{match.score.toFixed(3)}</span>
                  </li>
                ))}
              </ol>
            ) : <p className="text-ink-soft">Your matches will appear here after a crawl and scoring against your profile.</p>}
          </Panel>
          <div className="desk-secondary">
            <Panel title="This week" href="/tracker" linkLabel="Tracker">
              <dl className="space-y-2 text-sm">
                {([
                  ["Applications created", digest.applications_created], ["Submitted", digest.applications_submitted],
                  ["Replies", digest.replies_received], ["Follow-ups due", digest.follow_ups_due],
                ] as const).map(([label, value]) => (
                  <div key={label} className="flex justify-between gap-4">
                    <dt className="text-ink-soft">{label}</dt>
                    <dd className="font-sans text-sm tabular-nums">{value.toLocaleString("en-US")}</dd>
                  </div>
                ))}
              </dl>
            </Panel>
            <Panel title="Score calibration" href="/swipe" linkLabel="Rate">
              {calibration.enough_data ? (
                <dl className="space-y-2 text-sm">
                  <div className="flex justify-between gap-4"><dt className="text-ink-soft">Suggested min score</dt><dd className="font-mono text-xs tabular-nums">{(calibration.suggested_min_score)?.toLocaleString("en-US")}</dd></div>
                  <div className="flex justify-between gap-4"><dt className="text-ink-soft">Kept vs skipped</dt><dd className="font-mono text-xs tabular-nums">{(calibration.interested_mean)?.toLocaleString("en-US")} vs {(calibration.skipped_mean)?.toLocaleString("en-US")}</dd></div>
                </dl>
              ) : <p className="text-sm text-ink-soft">{calibration.decided.toLocaleString("en-US")} rated so far. The match threshold is set once you have kept ten postings, since fewer would be noise.</p>}
            </Panel>
          </div>
        </div>
        <Panel title="Pipeline" href="/applications" linkLabel="All applications">
          <LivePipeline initial={counts} order={STATUS_ORDER} />
          {recent.length ? (
            <ul className="mt-2 divide-y divide-rule-soft">
              {recent.map((application) => (
                <li key={application.id} className="flex min-h-11 min-w-0 flex-wrap items-center justify-between gap-x-4 gap-y-2 py-3">
                  <Link href={`/applications/${application.id}`} className="min-w-0 flex-1 basis-full truncate font-mono text-xs text-ink-soft hover:text-ink sm:basis-0">{application.url.replace(/^https?:\/\//, "")}</Link>
                  <StatusPill status={application.status} reason={application.failure_reason} />
                </li>
              ))}
            </ul>
          ) : null}
        </Panel>
      </div>
    </StatusStreamProvider>
  );
}
