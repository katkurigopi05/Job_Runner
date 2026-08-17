import { ApiError, NEEDS_OWNER, api, type Application } from "@/lib/api";
import { ReviewCard } from "./review-card";
import { ErrorPanel } from "@/components/error-panel";

export const dynamic = "force-dynamic";

export default async function ReviewPage() {
  let applications: Application[];
  try {
    applications = await api.applications();
  } catch (error) {
    if (error instanceof ApiError) return <ErrorPanel error={error} />;
    throw error;
  }

  const waiting = applications
    .filter((application) => NEEDS_OWNER.includes(application.status))
    .sort((a, b) => a.updated_at.localeCompare(b.updated_at));

  return (
    <div className="space-y-10">
      <header>
        <h1 className="font-display text-display leading-[1.05] tracking-tight">Review queue</h1>
        <p className="mt-3 max-w-prose text-ink-soft">
          {waiting.length === 0
            ? "Nothing is waiting on you."
            : `${waiting.length} application${waiting.length === 1 ? "" : "s"} filled and held. Oldest first — these are the ones nothing happens to until you decide.`}
        </p>
      </header>

      {waiting.length === 0 ? (
        <p className="border border-dashed border-rule px-6 py-16 text-center font-mono text-sm text-ink-faint">
          empty queue
        </p>
      ) : (
        <div className="space-y-8">
          {waiting.map((application) => (
            <ReviewCard key={application.id} application={application} />
          ))}
        </div>
      )}
    </div>
  );
}
