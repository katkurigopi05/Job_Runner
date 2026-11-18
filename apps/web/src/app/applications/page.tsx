import { ApiError, api, type Application, type Match } from "@/lib/api";
import { ApplicationsTable } from "./applications-table";
import { ErrorPanel } from "@/components/error-panel";
import { RefreshButton } from "@/components/refresh-button";

export const dynamic = "force-dynamic";

export default async function PipelinePage() {
  let applications: Application[];
  let matches: Match[];
  try {
    [applications, matches] = await Promise.all([api.applications(), api.matches(true).catch(() => [])]);
  } catch (error) {
    if (error instanceof ApiError) return <ErrorPanel error={error} />;
    throw error;
  }

  return (
    <div className="space-y-10">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-display leading-[1.05] tracking-tight">Pipeline</h1>
          <p className="mt-3 max-w-prose text-ink-soft">
            Every application, by where it stopped.
          </p>
        </div>
        <RefreshButton />
      </header>

      <ApplicationsTable applications={applications} matches={matches} />
    </div>
  );
}
