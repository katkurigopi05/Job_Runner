"use server";

import { revalidatePath } from "next/cache";
import { ApiError, api } from "@/lib/api";

export interface JudgmentResult {
  ok: boolean;
  message: string;
}

/**
 * Record a 0–2 grade for one posting against one question, from the server,
 * for the reason `recordGrade` beside `/label` gives: the API answers only
 * loopback callers and a browser fetch would need CORS opened.
 */
export async function recordJudgment(
  judgmentId: string,
  relevance: number,
): Promise<JudgmentResult> {
  try {
    await api.recordJudgment(judgmentId, relevance);
  } catch (error) {
    if (error instanceof ApiError) return { ok: false, message: error.message };
    throw error;
  }
  revalidatePath("/label/questions");
  return { ok: true, message: "graded" };
}
