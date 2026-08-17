import Link from "next/link";
import { ApiError, api, type Application, type ApplicationStatus } from "@/lib/api";
import { StatusPill } from "@/components/status";
import { ErrorPanel } from "@/components/error-panel";

export const dynamic = "force-dynamic";

// Reading order, not alphabetical: what needs you, then what is moving, then
// what is finished.
const COLUMNS: { status: ApplicationStatus; blurb: string }[] = [
  { status: "needs_review", blurb: "filled and held" },
  { status: "needs_otp", blurb: "waiting on a code" },
  { status: "running", blurb: "a worker has it" },
  { status: "queued", blurb: "not started" },
  { status: "submitted", blurb: "sent" },
  { status: "failed", blurb: "stopped" },
];

export default async function PipelinePage() {
  let applications: Application[];
  try {
    applications = await api.applications();
  } catch (error) {
    if (error instanceof ApiError) return <ErrorPanel error={error} />;
    throw error;
  }

  return (
    <div className="space-y-10">
      <header>
        <h1 className="font-display text-display leading-[1.05] tracking-tight">Pipeline</h1>
        <p className="mt-3 max-w-prose text-ink-soft">
          Every application, by where it stopped.
        </p>
      </header>

      <div className="space-y-10">
        {COLUMNS.map(({ status, blurb }) => {
          const rows = applications.filter((a) => a.status === status);
          if (rows.length === 0) return null;
          return (
            <section key={status} aria-labelledby={`col-${status}`}>
              <div className="flex items-baseline gap-4 border-b border-rule pb-3">
                <h2 id={`col-${status}`} className="font-display text-xl">
                  {status.replace(/_/g, " ")}
                </h2>
                <span className="font-mono text-xs text-ink-faint">{blurb}</span>
                <span className="ml-auto font-mono text-sm tabular-nums text-ink-soft">
                  {rows.length}
                </span>
              </div>
              <ul className="divide-y divide-rule">
                {rows.map((application) => (
                  <li key={application.id} className="flex flex-wrap items-center gap-4 py-4">
                    <Link
                      href={`/applications/${application.id}`}
                      className="min-w-0 flex-1 truncate underline-offset-4 hover:underline"
                    >
                      {application.url}
                    </Link>
                    <span className="font-mono text-xs text-ink-faint">
                      {application.ats ?? "unknown ats"}
                    </span>
                    <StatusPill status={application.status} reason={application.failure_reason} />
                  </li>
                ))}
              </ul>
            </section>
          );
        })}
      </div>
    </div>
  );
}
