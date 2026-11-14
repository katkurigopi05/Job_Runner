import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { ErrorPanel } from "@/components/error-panel";
import { RefreshButton } from "@/components/refresh-button";
import {
  ApiError,
  api,
  type Profile,
  type Project,
  type Resume,
  type StudioEvidence,
} from "@/lib/api";
import { StudioWorkspace } from "./studio-workspace";

export const dynamic = "force-dynamic";

export default async function ResumeStudioPage() {
  let candidateId = "";
  let profiles: Profile[] = [];
  let resumes: Resume[] = [];
  let projects: Project[] = [];
  let evidence: StudioEvidence | null = null;

  try {
    const [candidates, loadedProfiles] = await Promise.all([api.candidates(), api.profiles()]);
    profiles = loadedProfiles;
    candidateId = candidates[0]?.id ?? "";
    if (candidateId) {
      const [loadedResumes, loadedProjects] = await Promise.all([
        api.resumes(candidateId),
        api.projects(candidateId).catch(() => [] as Project[]),
      ]);
      resumes = loadedResumes;
      projects = loadedProjects;
    }
    const primaryProfile = profiles[0];
    if (primaryProfile) {
      evidence = await api.studioEvidence(primaryProfile.id).catch(() => null);
    }
  } catch (error) {
    if (error instanceof ApiError) return <ErrorPanel error={error} />;
    throw error;
  }

  const initialProfile = profiles[0];
  const defaultResume =
    resumes.find((r) => r.id === initialProfile?.base_resume_id) ??
    resumes.find((r) => r.is_default) ??
    resumes[0];

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <Link
            href="/resumes"
            className="mb-3 inline-flex items-center gap-1.5 font-mono text-xs text-ink-soft underline-offset-4 hover:text-ink hover:underline"
          >
            <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" />
            <span>Back to Résumés</span>
          </Link>
          <h1 className="font-display text-display leading-[1.05] tracking-tight">Resume Studio</h1>
          <p className="mt-3 max-w-prose text-ink-soft">
            Synthesize a role-targeted résumé from your base résumé, private &amp; public GitHub
            repositories, internships, and certifications — checked by ATS rules and the
            no-fabrication guard, with PDF and ATS DOCX export.
          </p>
        </div>
        <RefreshButton />
      </header>

      {candidateId ? (
        <StudioWorkspace
          candidateId={candidateId}
          profiles={profiles}
          resumes={resumes}
          projects={projects}
          initialProfileId={initialProfile?.id ?? ""}
          initialBaseResumeId={defaultResume?.id ?? ""}
          initialInternships={evidence?.internships ?? []}
          initialCertifications={evidence?.certifications ?? []}
        />
      ) : (
        <p className="border border-dashed border-rule px-6 py-16 text-center font-mono text-sm text-ink-soft">
          Create a candidate profile in Setup or run `make demo` first to use Resume Studio.
        </p>
      )}
    </div>
  );
}
