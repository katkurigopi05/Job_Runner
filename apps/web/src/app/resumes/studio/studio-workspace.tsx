"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { Download, Lock, Sparkles } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  ApiError,
  request,
  type Profile,
  type Project,
  type Resume,
} from "@/lib/api";
import {
  ROLE_TRACKS,
  studioApi,
  type Evidence,
  type RoleTrack,
  type StudioPreview,
  type StudioCodeApproval,
  type StudioRequest,
} from "@/lib/studio-api";
import { CodeEvidenceReview } from "./code-evidence";
import { BulletTrace, DraftView } from "./draft-view";
import { EvidenceEditor } from "./evidence-editor";
import { Readout } from "./readout";

const SELECT =
  "rounded-[var(--radius)] border border-rule bg-paper px-2.5 py-1.5 text-sm text-ink";

type StudioWorkspaceProps = {
  candidateId: string;
  profiles: Profile[];
  resumes: Resume[];
  projects: Project[];
  initialProfileId: string;
  initialBaseResumeId: string;
  initialEvidence: Evidence | null;
};

function messageOf(error: unknown): string {
  return error instanceof ApiError ? error.message : "Something went wrong.";
}

function printable(project: Project): boolean {
  return project.resume_listed !== false && project.include !== false;
}

export function StudioWorkspace(props: StudioWorkspaceProps) {
  const { candidateId, profiles, resumes, projects } = props;
  const [profileId, setProfileId] = useState(props.initialProfileId);
  const [baseResumeId, setBaseResumeId] = useState(props.initialBaseResumeId);
  const [track, setTrack] = useState<RoleTrack>("swe");
  const [posting, setPosting] = useState("");
  const [includeInternships, setIncludeInternships] = useState(true);
  const [skippedCerts, setSkippedCerts] = useState<string[]>([]);
  const [pickedProjects, setPickedProjects] = useState<string[]>([]);
  const [useModel, setUseModel] = useState(false);
  const [liveCode, setLiveCode] = useState(false);
  const [codeApprovals, setCodeApprovals] = useState<StudioCodeApproval[]>([]);
  const [liveProjects, setLiveProjects] = useState(true);
  const [replaceProjects, setReplaceProjects] = useState(true);
  const [modelProvider, setModelProvider] = useState<"configured" | "kaggle">("kaggle");
  const [asked, setAsked] = useState<StudioRequest | null>(null);
  const [preview, setPreview] = useState<StudioPreview | null>(null);
  const [sections, setSections] = useState<Record<string, string[]>>({});
  const [ownerEdits, setOwnerEdits] = useState<string[]>([]);
  const [adopt, setAdopt] = useState(false);
  const [saved, setSaved] = useState<Resume | null>(null);

  const evidence = useQuery({
    queryKey: ["studio-evidence", profileId],
    queryFn: () => studioApi.evidence(profileId),
    enabled: Boolean(profileId),
    initialData:
      profileId === props.initialProfileId
        ? (props.initialEvidence ?? undefined)
        : undefined,
  });
  const confirmedCerts = (evidence.data?.certifications ?? []).filter(
    (c) => c.verified && c.id,
  );

  const synthesize = useMutation({
    mutationFn: (body: StudioRequest) => studioApi.synthesize(body),
    onSuccess: (result, body) => {
      setAsked(body);
      setPreview(result);
      setSections(result.sections);
      setOwnerEdits([]);
      setCodeApprovals([]);
      setSaved(null);
    },
  });

  const save = useMutation({
    mutationFn: () => {
      if (!preview || !asked) throw new Error("Synthesize a draft first.");
      return studioApi.save({
        candidate_id: candidateId,
        profile_id: profileId || null,
        base_resume_id: preview.base_resume_id,
        role_track: asked.role_track,
        selected_project_ids: preview.projects
          .filter((p) => p.chosen)
          .map((p) => p.id),
        selected_certification_ids: asked.selected_certification_ids,
        live_projects: asked.live_projects,
        live_code: asked.live_code,
        code_approvals: codeApprovals,
        job_description: asked.live_code ? asked.job_description : undefined,
        project_rewrites: preview.projects.filter((p) => p.chosen && !asked.live_code).map((p) => ({
          project_id: p.id,
          original: p.original_description,
          tailored: p.tailored_description,
        })),
        include_internships: asked.include_internships,
        contact: preview.contact,
        sections,
        rewrites: preview.bullets
          .filter((b) => b.status === "rewritten")
          .map((b) => ({ original: b.original, tailored: b.tailored })),
        owner_edits: ownerEdits,
        tailored_by: preview.tailored_by,
        adopt: adopt && Boolean(profileId),
      });
    },
    onSuccess: (result) => setSaved(result.resume),
  });

  const makeBase = useMutation({
    mutationFn: (resumeId: string) =>
      request<Resume>(
        `/resumes/${resumeId}/set-base?profile_id=${encodeURIComponent(profileId)}`,
        {
          method: "POST",
        },
      ),
  });

  const run = () =>
    synthesize.mutate({
      candidate_id: candidateId,
      profile_id: profileId || null,
      base_resume_id: baseResumeId || null,
      job_description: posting,
      role_track: track,
      selected_project_ids: pickedProjects.length > 0 ? pickedProjects : null,
      selected_certification_ids: confirmedCerts
        .map((c) => c.id as string)
        .filter((id) => !skippedCerts.includes(id)),
      include_internships: includeInternships,
      use_model: useModel,
      live_projects: liveProjects && !liveCode,
      live_code: liveCode,
      model_provider: modelProvider,
      replace_source_projects: replaceProjects && !liveCode,
    });

  const toggle = (list: string[], id: string) =>
    list.includes(id) ? list.filter((x) => x !== id) : [...list, id];

  return (
    <div className="space-y-12">
      <section
        aria-labelledby="inputs-heading"
        className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_18rem]"
      >
        <div className="space-y-4">
          <h2 id="inputs-heading" className="font-display text-xl">
            The posting
          </h2>
          <div
            className="flex flex-wrap gap-1.5"
            role="radiogroup"
            aria-label="Role preset"
          >
            {ROLE_TRACKS.map((role) => (
              <button
                key={role.value}
                type="button"
                role="radio"
                aria-checked={track === role.value}
                onClick={() => setTrack(role.value)}
                className={`rounded-full border px-3 py-1 font-mono text-[11px] transition ${
                  track === role.value
                    ? "border-ink bg-ink text-paper"
                    : "border-rule text-ink-soft hover:text-ink"
                }`}
              >
                {role.label}
              </button>
            ))}
          </div>
          <p className="text-xs text-ink-faint">
            The job’s direct requirements rank projects first. The preset helps
            with postings that do not state requirements clearly.
          </p>
          <label className="grid gap-1 text-xs text-ink-soft">
            Paste the job description
            <textarea
              className="min-h-56 rounded-[var(--radius)] border border-rule bg-paper px-3 py-2 text-sm text-ink"
              value={posting}
              onChange={(event) => setPosting(event.target.value)}
            />
          </label>
        </div>

        <div className="space-y-4 text-sm">
          <label className="grid gap-1 text-xs text-ink-soft">
            Profile
            <select
              className={SELECT}
              value={profileId}
              onChange={(e) => setProfileId(e.target.value)}
            >
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.label}
                </option>
              ))}
            </select>
          </label>
          <label className="grid gap-1 text-xs text-ink-soft">
            Start from
            <select
              className={SELECT}
              value={baseResumeId}
              onChange={(e) => setBaseResumeId(e.target.value)}
            >
              {resumes.map((r) => (
                <option key={r.id} value={r.id}>
                  v{r.version}
                  {r.is_default ? " (default)" : ""}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              className="accent-go"
              checked={includeInternships}
              onChange={(e) => setIncludeInternships(e.target.checked)}
            />
            Confirmed internships
          </label>
          <fieldset className="space-y-1">
            <legend className="text-xs text-ink-soft">
              Confirmed certifications
            </legend>
            {confirmedCerts.length === 0 ? (
              <p className="text-xs text-ink-faint">None confirmed yet.</p>
            ) : null}
            {confirmedCerts.map((cert) => (
              <label key={cert.id} className="flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  className="accent-go"
                  checked={!skippedCerts.includes(cert.id as string)}
                  onChange={() =>
                    setSkippedCerts((list) => toggle(list, cert.id as string))
                  }
                />
                {cert.name}
              </label>
            ))}
          </fieldset>
          <fieldset className="space-y-1">
            <legend className="text-xs text-ink-soft">
              Projects (forks excluded; none ticked: ranked for the posting)
            </legend>
            {projects.filter((project) => !project.is_fork).map((project) => (
              <label
                key={project.id}
                className={`flex items-center gap-2 text-xs ${printable(project) ? "" : "text-ink-faint"}`}
              >
                <input
                  type="checkbox"
                  className="accent-go"
                  disabled={!printable(project)}
                  checked={pickedProjects.includes(project.id)}
                  onChange={() =>
                    setPickedProjects((list) => toggle(list, project.id))
                  }
                />
                {project.is_private ? (
                  <Lock className="h-3 w-3" aria-label="private" />
                ) : null}
                {project.name}
                {printable(project) ? null : (
                  <span>(evidence only: include or pin it on Résumés)</span>
                )}
              </label>
            ))}
          </fieldset>
          <label className="flex items-start gap-2 text-xs">
            <input type="checkbox" className="mt-0.5 accent-go" checked={replaceProjects && !liveCode} disabled={liveCode}
              onChange={(e) => setReplaceProjects(e.target.checked)} />
            <span>Use ranked GitHub projects as this draft’s Projects section.
              This replaces the base résumé’s project entries with the selected descriptions.
              Review the print preview before saving.</span>
          </label>
          <label className="flex items-start gap-2 text-xs">
            <input type="checkbox" className="mt-0.5 accent-go" checked={liveProjects && !liveCode} disabled={liveCode}
              onChange={(e) => setLiveProjects(e.target.checked)} />
            <span>Check current public GitHub projects before ranking (up to 20).
              Failed checks are left out of this catalog check.</span>
          </label>
          <label className="flex items-start gap-2 text-xs">
            <input type="checkbox" className="mt-0.5 accent-go" checked={liveCode}
              onChange={(e) => {
                setLiveCode(e.target.checked);
                if (e.target.checked) setModelProvider("configured");
              }} />
            <span>Use live GitHub code evidence</span>
          </label>
          <label className="flex items-start gap-2 text-xs">
            <input
              type="checkbox"
              className="mt-0.5 accent-go"
              checked={useModel}
              onChange={(e) => setUseModel(e.target.checked)}
            />
            <span>
              {liveCode
                ? "Revise supported résumé bullets for this job."
                : "Rewrite selected project descriptions for this job and revise supported résumé bullets."}
              Each rewrite is checked against its own source; unsupported claims keep the original.
            </span>
          </label>
          {useModel ? (
            <label className="block text-xs">Writing model
              <select className="ml-2 rounded border border-rule bg-paper p-2"
                value={modelProvider}
                onChange={(e) => setModelProvider(e.target.value as "configured" | "kaggle")}>
                <option value="kaggle">Kaggle 27B</option>
                <option value="configured">Configured tailoring model</option>
              </select>
            </label>
          ) : null}
          <Button
            variant="accent"
            size="lg"
            disabled={!posting.trim() || synthesize.isPending}
            onClick={run}
          >
            <Sparkles aria-hidden />{" "}
            {synthesize.isPending ? "Working…" : "Synthesize draft"}
          </Button>
          {synthesize.error ? (
            <p className="text-xs text-stop" role="alert">
              {messageOf(synthesize.error)}
            </p>
          ) : null}
        </div>
      </section>

      {preview ? (
        <>
          {preview.code_review ? (
            <CodeEvidenceReview
              review={preview.code_review}
              approvals={codeApprovals}
              onChange={setCodeApprovals}
            />
          ) : null}
          <div className="grid gap-10 lg:grid-cols-[minmax(0,1fr)_20rem]">
            <DraftView
              preview={preview}
              sections={sections}
              ownerEdits={ownerEdits}
              onChange={(next, edits) => {
                setSections(next);
                setOwnerEdits(edits);
              }}
            />
            <Readout preview={preview} />
          </div>
          <section aria-labelledby="trace-heading" className="space-y-3">
            <h2 id="trace-heading" className="font-display text-xl">
              Each bullet, and what backs it
            </h2>
            <p className="text-xs text-ink-soft">
              {preview.calls > 0
                ? `${preview.calls} model call(s), answered by ${preview.tailored_by ?? "an unrecorded model"}.`
                : "No model was asked."}
            </p>
            <BulletTrace bullets={preview.bullets} />
          </section>
          <details className="border-t border-rule pt-4">
            <summary className="cursor-pointer font-mono text-xs text-ink-soft">
              Print preview (as generated, before your edits)
            </summary>
            <iframe
              title="Print preview"
              sandbox=""
              srcDoc={`<body style="font-family:sans-serif;font-size:13px;max-width:46rem">${preview.html}</body>`}
              className="mt-3 h-[36rem] w-full rounded-[var(--radius)] border border-rule bg-white"
            />
          </details>
          <section
            aria-labelledby="save-heading"
            className="flex flex-wrap items-center gap-4 border-t border-rule pt-6"
          >
            <h2 id="save-heading" className="sr-only">
              Save
            </h2>
            <label className="flex items-center gap-2 text-xs">
              <input
                type="checkbox"
                className="accent-go"
                checked={adopt}
                disabled={!profileId}
                onChange={(e) => setAdopt(e.target.checked)}
              />
              Make it this profile&rsquo;s base résumé
            </label>
            <Button
              variant="default"
              disabled={save.isPending || synthesize.isPending || codeApprovals.length > 12}
              onClick={() => save.mutate()}
            >
              {save.isPending ? "Saving…" : "Save as a new version"}
            </Button>
            {save.error ? (
              <p className="text-xs text-stop" role="alert">
                {messageOf(save.error)}
              </p>
            ) : null}
            {saved ? (
              <div
                className="flex flex-wrap items-center gap-3 text-xs"
                role="status"
              >
                <span>Saved as v{saved.version}.</span>
                <a
                  className="inline-flex items-center gap-1 underline-offset-4 hover:underline"
                  href={`/api/resumes/${saved.id}/file`}
                >
                  <Download className="h-3 w-3" aria-hidden /> PDF
                </a>
                <a
                  className="inline-flex items-center gap-1 underline-offset-4 hover:underline"
                  href={`/api/resumes/${saved.id}/docx`}
                >
                  <Download className="h-3 w-3" aria-hidden /> DOCX
                </a>
                {!adopt && profileId ? (
                  <Button
                    size="sm"
                    disabled={makeBase.isPending || makeBase.isSuccess}
                    onClick={() => makeBase.mutate(saved.id)}
                  >
                    {makeBase.isSuccess
                      ? "Now the profile's base"
                      : "Make it the profile's base"}
                  </Button>
                ) : null}
              </div>
            ) : null}
          </section>
        </>
      ) : null}

      {profileId && evidence.data ? (
        <EvidenceEditor
          key={profileId + evidence.dataUpdatedAt}
          profileId={profileId}
          evidence={evidence.data}
          onSaved={() => void evidence.refetch()}
        />
      ) : null}
    </div>
  );
}
