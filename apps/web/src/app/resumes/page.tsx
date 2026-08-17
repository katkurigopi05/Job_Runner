import { ApiError, api, type Profile, type Resume, type ResumeParsed } from "@/lib/api";
import { ResumePreview } from "@/components/resume-preview";
import { ErrorPanel } from "@/components/error-panel";

export const dynamic = "force-dynamic";

export default async function ResumesPage() {
  let resumes: Resume[];
  let profiles: Profile[];
  try {
    // Résumés are listed per candidate, so the candidate list comes first.
    const [candidates, loadedProfiles] = await Promise.all([api.candidates(), api.profiles()]);
    profiles = loadedProfiles;
    const perCandidate = await Promise.all(
      candidates.map((candidate) => api.resumes(candidate.id)),
    );
    resumes = perCandidate.flat();
  } catch (error) {
    if (error instanceof ApiError) return <ErrorPanel error={error} />;
    throw error;
  }

  const parsed = new Map<string, ResumeParsed>();
  await Promise.all(
    resumes.map(async (resume) => {
      try {
        parsed.set(resume.id, await api.resumeParsed(resume.id));
      } catch {
        // Shown as unparseable below rather than taking the page down. A
        // résumé that will not parse is exactly what this screen is for.
      }
    }),
  );

  const baseFor = (resumeId: string) =>
    profiles.filter((profile) => profile.base_resume_id === resumeId).map((p) => p.label);

  return (
    <div className="space-y-10">
      <header>
        <h1 className="font-display text-display leading-[1.05] tracking-tight">Résumés</h1>
        <p className="mt-3 max-w-prose text-ink-soft">
          Shown as the parser reads them, which is what an ATS gets. A section missing here is
          missing from every application that sends this file.
        </p>
      </header>

      {resumes.length === 0 ? (
        <p className="border border-dashed border-rule px-6 py-16 text-center font-mono text-sm text-ink-faint">
          no résumés uploaded
        </p>
      ) : (
        <div className="space-y-12">
          {[...resumes]
            .sort((a, b) => b.version - a.version)
            .map((resume) => {
              const usedBy = baseFor(resume.id);
              const document = parsed.get(resume.id);
              return (
                <section key={resume.id} aria-labelledby={`r-${resume.id}`}>
                  <div className="flex flex-wrap items-baseline gap-4 border-b border-rule pb-3">
                    <h2 id={`r-${resume.id}`} className="font-display text-xl">
                      v{resume.version}
                    </h2>
                    {resume.is_default ? (
                      <span className="rounded-full border border-go/40 bg-go-soft px-3 py-0.5 font-mono text-xs text-go">
                        default
                      </span>
                    ) : null}
                    {usedBy.length > 0 ? (
                      <span className="font-mono text-xs text-ink-soft">
                        base for {usedBy.join(", ")}
                      </span>
                    ) : null}
                    <a
                      href={`/api/resumes/${resume.id}/file`}
                      className="ml-auto font-mono text-xs text-ink-soft underline-offset-4 hover:text-ink hover:underline"
                    >
                      original file
                    </a>
                  </div>

                  <div className="mt-5">
                    {document ? (
                      <ResumePreview parsed={document} />
                    ) : (
                      <p className="border border-stop/40 bg-stop-soft px-5 py-4 text-sm text-stop">
                        This résumé could not be parsed. An ATS will not read it either — re-upload
                        it as text-based PDF, DOCX, or TXT rather than a scan.
                      </p>
                    )}
                  </div>
                </section>
              );
            })}
        </div>
      )}
    </div>
  );
}
