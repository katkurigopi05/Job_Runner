import Link from "next/link";
import { Sparkles } from "lucide-react";
import {
  ApiError,
  api,
  type Profile,
  type Project,
  type ProjectEntries,
  type Resume,
  type ResumeParsed,
} from "@/lib/api";
import { ErrorPanel } from "@/components/error-panel";
import { ProjectLinks } from "@/components/project-links";
import { RefreshButton } from "@/components/refresh-button";
import { ResumeUpload } from "@/components/resume-upload";
import { ResumeEditor } from "@/components/resume-editor";

/** The GitHub account a résumé links to in its contact line, if any. */
function githubUserOf(document: ResumeParsed | undefined): string {
  const links = (document?.parsed.contact?.links ?? []) as unknown[];
  for (const link of links) {
    const match = typeof link === "string" ? /github\.com\/([A-Za-z0-9-]+)/i.exec(link) : null;
    if (match) return match[1];
  }
  return "";
}

export const dynamic = "force-dynamic";

export default async function ResumesPage() {
  let resumes: Resume[];
  let profiles: Profile[];
  let candidateId = "";
  try {
    // Résumés are listed per candidate, so the candidate list comes first.
    const [candidates, loadedProfiles] = await Promise.all([api.candidates(), api.profiles()]);
    profiles = loadedProfiles;
    candidateId = candidates[0]?.id ?? "";
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

  // Links are made against the résumé applications send: the profile's base,
  // else the default, else the newest.
  const linkTarget =
    resumes.find((resume) => resume.id === profiles[0]?.base_resume_id) ??
    resumes.find((resume) => resume.is_default) ??
    [...resumes].sort((a, b) => b.version - a.version)[0];
  let projects: Project[] = [];
  let entries: ProjectEntries = { entries: [], suggestions: {} };
  if (candidateId && linkTarget) {
    try {
      [projects, entries] = await Promise.all([
        api.projects(candidateId),
        api.projectEntries(linkTarget.id),
      ]);
    } catch {
      // The rest of the page does not depend on this panel.
    }
  }

  return (
    <div className="space-y-10">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-display leading-[1.05] tracking-tight">Résumés</h1>
          <p className="mt-3 max-w-prose text-ink-soft">
            Shown as the parser reads them, which is what an ATS gets. A section missing here is
            missing from every application that sends this file.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Link
            href="/resumes/studio"
            className="inline-flex h-8 items-center gap-1.5 rounded-full border border-brand bg-brand px-3.5 font-mono text-[11px] font-medium text-on-accent shadow-xs transition-opacity hover:opacity-90"
          >
            <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
            <span>Open Studio</span>
          </Link>
          <RefreshButton />
        </div>
      </header>

      {candidateId ? (
        <ResumeUpload candidateId={candidateId} profileId={profiles[0]?.id} />
      ) : null}

      {candidateId && linkTarget ? (
        <ProjectLinks
          candidateId={candidateId}
          githubUser={githubUserOf(parsed.get(linkTarget.id))}
          projects={projects}
          entries={entries.entries}
          suggestions={entries.suggestions}
        />
      ) : null}

      {resumes.length === 0 ? (
        <p className="border border-dashed border-rule px-6 py-16 text-center text-sm text-ink-faint">
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
                    <h2 id={`r-${resume.id}`} className="text-xl">
                      V{(resume.version).toLocaleString("en-US")}
                    </h2>
                    {resume.is_default ? (
                      <span className="inline-flex items-center gap-2 text-label text-ink-soft">
                        default
                      </span>
                    ) : null}
                    {usedBy.length > 0 ? (
                      <span className="text-xs text-ink-soft">
                        base for {usedBy.join(", ")}
                      </span>
                    ) : null}
                    <div className="ml-auto flex items-center gap-4">
                      <a
                        href={`/api/resumes/${resume.id}/docx`}
                        className="text-xs text-ink underline-offset-4 hover:underline"
                      >
                        download .docx
                      </a>
                      <a
                        href={`/api/resumes/${resume.id}/file`}
                        className="text-xs text-ink-soft underline-offset-4 hover:text-ink hover:underline"
                      >
                        original file
                      </a>
                    </div>
                  </div>

                  <div className="mt-5">
                    {/* `ResumeEditor` renders the preview when it is closed and
                        becomes the editable document when opened, so there is
                        one résumé on screen rather than a copy to compare
                        against. A file the parser could not read has no
                        sections to edit, and offering an editor over nothing
                        would suggest it is fixable here when it needs
                        re-uploading. */}
                    {document ? (
                      <ResumeEditor parsed={document} />
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
