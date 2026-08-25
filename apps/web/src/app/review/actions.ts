"use server";

import { revalidatePath } from "next/cache";
import { ApiError, api } from "@/lib/api";

export interface ReviewResult {
  ok: boolean;
  message: string;
}

/**
 * Approve an application, carrying whatever the owner typed for the questions
 * the agent could not answer.
 *
 * The answers are passed through exactly as entered. CLAUDE.md §2.2 — these go
 * onto a real application and some of them have legal weight, so nothing here
 * rewrites, expands, or "cleans up" what the owner wrote.
 */
export async function approve(
  applicationId: string,
  _prev: ReviewResult | null,
  form: FormData,
): Promise<ReviewResult> {
  const answers: Record<string, string> = {};
  for (const [field, value] of form.entries()) {
    if (!field.startsWith("answer:")) continue;
    const text = String(value).trim();
    if (text) answers[field.slice("answer:".length)] = text;
  }

  const note = String(form.get("note") ?? "").trim();

  try {
    await api.review(applicationId, { approve: true, answers, note: note || undefined });
  } catch (error) {
    if (error instanceof ApiError) return { ok: false, message: error.message };
    throw error;
  }

  revalidatePath("/review");
  revalidatePath("/applications");
  revalidatePath("/");
  return { ok: true, message: "Approved. The worker will resume and submit it." };
}

export async function reject(
  applicationId: string,
  _prev: ReviewResult | null,
  form: FormData,
): Promise<ReviewResult> {
  const note = String(form.get("note") ?? "").trim();

  try {
    await api.review(applicationId, { approve: false, note: note || undefined });
  } catch (error) {
    if (error instanceof ApiError) return { ok: false, message: error.message };
    throw error;
  }

  revalidatePath("/review");
  revalidatePath("/applications");
  revalidatePath("/");
  return { ok: true, message: "Rejected. Nothing was sent." };
}

export async function submitOtp(
  applicationId: string,
  _prev: ReviewResult | null,
  form: FormData,
): Promise<ReviewResult> {
  const code = String(form.get("code") ?? "").trim();
  if (!code) return { ok: false, message: "Enter the code the site sent you." };

  try {
    await api.otp(applicationId, code);
  } catch (error) {
    if (error instanceof ApiError) return { ok: false, message: error.message };
    throw error;
  }

  revalidatePath("/review");
  revalidatePath("/applications");
  return { ok: true, message: "Code accepted. The run resumed." };
}

/**
 * Tailor this posting a second time with the other provider, for a comparison.
 *
 * On demand rather than on every application, and that is a §2.8 decision: each
 * remote side is another upload of the owner's résumé to a third party. The
 * tailoring cache means asking twice for the same posting sends nothing.
 */
export async function compareTailoring(
  applicationId: string,
  _prev: ReviewResult | null,
  _form: FormData,
): Promise<ReviewResult> {
  try {
    await api.compareTailoring(applicationId);
  } catch (error) {
    if (error instanceof ApiError) return { ok: false, message: error.message };
    throw error;
  }

  revalidatePath("/review");
  return { ok: true, message: "Compared. Both versions are below." };
}

/**
 * Choose the version that gets uploaded.
 *
 * The id is validated server-side against the two that were actually compared —
 * this decides the file an employer receives.
 */
export async function selectTailoring(
  applicationId: string,
  _prev: ReviewResult | null,
  form: FormData,
): Promise<ReviewResult> {
  const resumeId = String(form.get("resume_id") ?? "").trim();
  if (!resumeId) return { ok: false, message: "No version was chosen." };

  try {
    await api.selectTailoring(applicationId, resumeId);
  } catch (error) {
    if (error instanceof ApiError) return { ok: false, message: error.message };
    throw error;
  }

  revalidatePath("/review");
  return { ok: true, message: "This version will be the one uploaded." };
}
