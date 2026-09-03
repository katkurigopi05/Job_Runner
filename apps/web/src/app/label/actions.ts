"use server";

import { revalidatePath } from "next/cache";
import { ApiError, api } from "@/lib/api";

export interface GradeResult {
  ok: boolean;
  message: string;
}

/**
 * Record a 0–3 grade, from the server.
 *
 * A Server Action for the same reason `/swipe` uses one: the API has no
 * authentication and refuses non-loopback callers, so a browser fetch from the
 * dashboard origin gets a CORS preflight the API answers with 405. Opening
 * CORS would widen exactly the surface that rule protects.
 */
export async function recordGrade(
  postingId: string,
  relevance: number,
  note?: string,
): Promise<GradeResult> {
  try {
    await api.recordLabel(postingId, relevance, note);
  } catch (error) {
    if (error instanceof ApiError) return { ok: false, message: error.message };
    throw error;
  }

  revalidatePath("/label");
  return { ok: true, message: "graded" };
}
