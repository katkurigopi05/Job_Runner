import {
  ApiError,
  NEEDS_OWNER,
  api,
  type Application,
  type Profile,
  type ResumeParsed,
} from "@/lib/api";
import { ReviewCard } from "./review-card";
import { ErrorPanel } from "@/components/error-panel";
import { AssistantDock } from "@/components/assistant-dock";

export const dynamic = "force-dynamic";

/**
 * Which résumé this application will actually attach.
 *
 * The tailored one when tailoring has run, otherwise the profile's base. The
 * distinction matters on this screen: approving sends a specific document, and
 * the owner should be looking at that one rather than at whichever résumé
 * happens to be default.
 */
function attachedResumeId(application: Application, profiles: Profile[]): string | null {
  if (application.tailored_resume_id) return application.tailored_resume_id;
  const profile = profiles.find((candidate) => candidate.id === application.profile_id);
  return profile?.base_resume_id ?? null;
}

export default async function ReviewPage() {
  let applications: Application[];
  let profiles: Profile[];
  try {
    [applications, profiles] = await Promise.all([api.applications(), api.profiles()]);
  } catch (error) {
    if (error instanceof ApiError) return <ErrorPanel error={error} />;
    throw error;
  }

  const waiting = applications
    .filter((application) => NEEDS_OWNER.includes(application.status))
    .sort((a, b) => a.updated_at.localeCompare(b.updated_at));

  // One fetch per distinct résumé, not one per application — several parked
  // applications usually share the same base.
  const wanted = [...new Set(waiting.map((a) => attachedResumeId(a, profiles)).filter(Boolean))];
  const parsed = new Map<string, ResumeParsed>();
  await Promise.all(
    wanted.map(async (id) => {
      try {
        parsed.set(id as string, await api.resumeParsed(id as string));
      } catch {
        // A résumé that will not parse is worth showing as missing rather than
        // failing the whole queue — the rest of the review still stands.
      }
    }),
  );

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
          {waiting.map((application) => {
            const resumeId = attachedResumeId(application, profiles);
            return (
              <ReviewCard
                key={application.id}
                application={application}
                resume={resumeId ? (parsed.get(resumeId) ?? null) : null}
                tailored={Boolean(application.tailored_resume_id)}
              />
            );
          })}
        </div>
      )}

      <AssistantDock />
    </div>
  );
}
