import Link from "next/link";
import { ApiError, NEEDS_OWNER, api, type Application, type ApplicationStatus } from "@/lib/api";
import { StatusPill } from "@/components/status";
import { ErrorPanel } from "@/components/error-panel";

export const dynamic = "force-dynamic";

const ORDER: ApplicationStatus[] = [
  "needs_review",
  "needs_otp",
  "running",
  "queued",
  "submitted",
  "failed",
];

export default async function DeskPage() {
  let applications: Application[];
  try {
    applications = await api.applications();
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
    .slice(0, 6);

  return (
    <div className="space-y-14">
      <header>
        <h1 className="font-display text-display leading-[1.05] tracking-tight">
          {waiting.length > 0 ? (
            <>
              {waiting.length} waiting
              <br />
              on you
            </>
          ) : (
            <>Nothing waiting on you</>
          )}
        </h1>
        {waiting.length > 0 ? (
          <Link
            href="/review"
            className="mt-6 inline-block bg-attn px-6 py-3 font-mono text-xs uppercase tracking-widest text-paper transition-opacity hover:opacity-85 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn"
          >
            open the queue
          </Link>
        ) : (
          <p className="mt-3 max-w-prose text-ink-soft">
            The worker parks anything it cannot finish. An empty desk means it is either working or
            done.
          </p>
        )}
      </header>

      <section aria-labelledby="tally">
        <h2 id="tally" className="font-mono text-xs uppercase tracking-widest text-ink-faint">
          Tally
        </h2>
        <dl className="mt-5 grid grid-cols-2 gap-px border border-rule bg-rule sm:grid-cols-3 lg:grid-cols-6">
          {ORDER.map((status) => (
            <div key={status} className="bg-paper-raised px-4 py-5">
              <dt className="font-mono text-xs text-ink-faint">{status.replace(/_/g, " ")}</dt>
              <dd className="mt-2 font-display text-3xl tabular-nums">{counts.get(status) ?? 0}</dd>
            </div>
          ))}
        </dl>
      </section>

      {recent.length > 0 ? (
        <section aria-labelledby="recent">
          <h2 id="recent" className="font-mono text-xs uppercase tracking-widest text-ink-faint">
            Latest movement
          </h2>
          <ul className="mt-5 divide-y divide-rule border-y border-rule">
            {recent.map((application) => (
              <li key={application.id} className="flex flex-wrap items-center gap-4 py-4">
                <Link
                  href={`/applications/${application.id}`}
                  className="min-w-0 flex-1 truncate underline-offset-4 hover:underline"
                >
                  {application.url}
                </Link>
                <StatusPill status={application.status} reason={application.failure_reason} />
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
