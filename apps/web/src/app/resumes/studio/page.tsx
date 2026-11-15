import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { ErrorPanel } from "@/components/error-panel";
import {
  ApiError,
  api,
  type Profile,
  type Project,
  type Resume,
} from "@/lib/api";
import { studioApi, type Evidence } from "@/lib/studio-api";
import { StudioWorkspace } from "./studio-workspace";

export const dynamic = "force-dynamic";

export default async function ResumeStudioPage() {
  let candidateId = "";
  let profiles: Profile[] = [];
  let resumes: Resume[] = [];
  let projects: Project[] = [];
  let evidence: Evidence | null = null;

  try {
    const [candidates, loadedProfiles] = await Promise.all([
      api.candidates(),
      api.profiles(),
    ]);
    candidateId = candidates[0]?.id ?? "";
    profiles = loadedProfiles.filter(
      (profile) => profile.candidate_id === candidateId,
    );
    if (candidateId) {
      [resumes, projects] = await Promise.all([
        api.resumes(candidateId),
        api.projects(candidateId),
      ]);
    }
    if (profiles[0]) evidence = await studioApi.evidence(profiles[0].id);
  } catch (error) {
    if (error instanceof ApiError) return <ErrorPanel error={error} />;
    throw error;
  }

  const profile = profiles[0];
  const base =
    resumes.find((r) => r.id === profile?.base_resume_id) ??
    resumes.find((r) => r.is_default) ??
    resumes[0];

  return (
    <div className="space-y-10">
      <header>
        <Link
          href="/resumes"
          className="mb-3 inline-flex items-center gap-1.5 font-mono text-xs text-ink-soft underline-offset-4 hover:text-ink hover:underline"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" />
          Résumés
        </Link>
        <h1 className="font-display text-display leading-[1.05] tracking-tight">
          Resume Studio
        </h1>
        <p className="mt-3 max-w-prose text-ink-soft">
          A draft for one posting, made from your résumé, the internships and
          certifications you confirm below, and your GitHub projects. Every line
          says where it came from, and a model is asked only for bullets that
          can truthfully gain a term the posting asks for.
        </p>
      </header>

      {candidateId && base ? (
        <StudioWorkspace
          candidateId={candidateId}
          profiles={profiles}
          resumes={resumes}
          projects={projects}
          initialProfileId={profile?.id ?? ""}
          initialBaseResumeId={base.id}
          initialEvidence={evidence}
        />
      ) : (
        <p className="border border-dashed border-rule px-6 py-16 text-center text-sm text-ink-soft">
          Upload a résumé on the{" "}
          <Link href="/resumes" className="underline">
            Résumés
          </Link>{" "}
          page first: the studio starts from one.
        </p>
      )}
    </div>
  );
}
