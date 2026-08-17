"use server";

import { revalidatePath } from "next/cache";

const API = process.env.JOBRUNNER_API ?? "http://127.0.0.1:8000";

export interface UploadResult {
  ok: boolean;
  message: string;
}

/**
 * Upload a résumé and optionally make it a profile's base.
 *
 * The file is streamed through this server rather than posted from the
 * browser: the API refuses non-loopback callers and has no CORS layer, and a
 * résumé is PII that should not take a detour it does not need (§2.8).
 *
 * Parsing happens at upload, so a file the parser cannot read fails here —
 * while you are watching — rather than mid-application later.
 */
export async function uploadResume(
  _prev: UploadResult | null,
  form: FormData,
): Promise<UploadResult> {
  const file = form.get("file");
  const candidateId = String(form.get("candidate_id") ?? "");
  const profileId = String(form.get("profile_id") ?? "");

  if (!(file instanceof File) || file.size === 0) {
    return { ok: false, message: "Choose a file first." };
  }
  if (!candidateId) {
    return { ok: false, message: "No candidate to attach this to." };
  }

  const upload = new FormData();
  upload.set("candidate_id", candidateId);
  upload.set("file", file, file.name);
  upload.set("is_default", "true");

  const created = await fetch(`${API}/resumes`, { method: "POST", body: upload }).catch(() => null);
  if (created === null) {
    return { ok: false, message: `Cannot reach the API at ${API}. Is \`make api\` running?` };
  }
  if (!created.ok) {
    const body = await created.json().catch(() => null);
    return { ok: false, message: body?.error?.message ?? `Upload failed (${created.status}).` };
  }

  const resume = await created.json();

  // A résumé nothing points at is a résumé no application will send.
  if (profileId) {
    const linked = await fetch(
      `${API}/resumes/${resume.id}/set-base?profile_id=${encodeURIComponent(profileId)}`,
      { method: "POST" },
    ).catch(() => null);
    if (linked === null || !linked.ok) {
      return {
        ok: false,
        message: `Uploaded as v${resume.version}, but could not set it as the profile's base.`,
      };
    }
  }

  revalidatePath("/resumes");
  revalidatePath("/review");
  return {
    ok: true,
    message: `Uploaded as v${resume.version} and set as the base résumé. Check the parse below — what you see is what an ATS gets.`,
  };
}
