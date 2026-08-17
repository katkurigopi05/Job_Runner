"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";
import { uploadResume, type UploadResult } from "@/app/resumes/actions";

function Submit() {
  const { pending } = useFormStatus();
  return (
    <button
      type="submit"
      disabled={pending}
      className="rounded-[var(--radius)] bg-ink px-5 py-2 font-mono text-xs uppercase tracking-widest text-paper transition-opacity hover:opacity-85 disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn"
    >
      {pending ? "parsing…" : "upload"}
    </button>
  );
}

export function ResumeUpload({
  candidateId,
  profileId,
}: {
  candidateId: string;
  profileId?: string;
}) {
  const [state, run] = useActionState<UploadResult | null, FormData>(uploadResume, null);

  return (
    <form
      action={run}
      className="rounded-[var(--radius-lg)] border border-dashed border-rule bg-paper-raised p-5"
    >
      <input type="hidden" name="candidate_id" value={candidateId} />
      {profileId ? <input type="hidden" name="profile_id" value={profileId} /> : null}

      <label htmlFor="resume-file" className="font-mono text-xs uppercase tracking-widest text-ink-soft">
        Upload a résumé
      </label>
      <p className="mt-1 max-w-prose text-sm text-ink-soft">
        PDF, DOCX, or TXT. It is parsed on upload and becomes this profile&apos;s base, so a file
        the parser cannot read fails here rather than mid-application. Scanned PDFs do not parse —
        an ATS cannot read those either.
      </p>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <input
          id="resume-file"
          type="file"
          name="file"
          accept=".pdf,.docx,.txt,.md"
          required
          className="max-w-full text-sm file:mr-3 file:rounded-[var(--radius)] file:border file:border-rule file:bg-paper-high file:px-3 file:py-1.5 file:font-mono file:text-xs file:text-ink"
        />
        <Submit />
      </div>

      {state ? (
        <p role="status" className={`mt-4 text-sm ${state.ok ? "text-go" : "text-stop"}`}>
          {state.message}
        </p>
      ) : null}
    </form>
  );
}
