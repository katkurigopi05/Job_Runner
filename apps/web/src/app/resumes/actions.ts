"use server";

import { revalidatePath } from "next/cache";
import { ApiError, api } from "@/lib/api";

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


/**
 * Save an edited résumé.
 *
 * The lines arrive as one textarea per section, split on newlines. Blank lines
 * are dropped server-side rather than here, so what the owner sees in the box
 * is what the server judges — a client that pre-trimmed would disagree with the
 * emptiness check and refuse an edit the screen showed as filled.
 */
export async function saveResumeEdit(
  resumeId: string,
  _prev: UploadResult | null,
  form: FormData,
): Promise<UploadResult> {
  const sections: Record<string, string[]> = {};
  for (const [field, value] of form.entries()) {
    if (!field.startsWith("section:")) continue;
    sections[field.slice("section:".length)] = String(value).split("\n");
  }

  const contact = {
    name: String(form.get("contact:name") ?? "").trim(),
    email: String(form.get("contact:email") ?? "").trim(),
    phone: String(form.get("contact:phone") ?? "").trim(),
    links: String(form.get("contact:links") ?? "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean),
  };

  try {
    const saved = await api.editResume(resumeId, { contact, sections });
    revalidatePath("/resumes");
    revalidatePath("/review");
    return {
      ok: true,
      message: `Saved as v${saved.version}. Profiles using the old one now use this.`,
    };
  } catch (error) {
    if (error instanceof ApiError) return { ok: false, message: error.message };
    throw error;
  }
}
