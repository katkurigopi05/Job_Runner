"use server";

import { revalidatePath } from "next/cache";
import { ApiError, api, type RankingKind } from "@/lib/api";

export type RankingResult = { ok: true } | { ok: false; message: string };

async function run(work: () => Promise<unknown>): Promise<RankingResult> {
  try {
    await work();
  } catch (error) {
    if (error instanceof ApiError) return { ok: false, message: error.message };
    throw error;
  }
  revalidatePath("/ranking");
  revalidatePath("/matches");
  return { ok: true };
}

/** Save or replace one explicit adjustment. The base score is never touched. */
export async function saveAdjustment(input: {
  kind: RankingKind;
  value: string;
  weight: number;
  source?: "explicit" | "suggestion";
}): Promise<RankingResult> {
  if (!Number.isFinite(input.weight) || input.weight < -0.3 || input.weight > 0.3) {
    return { ok: false, message: "weight must be between -0.30 and +0.30" };
  }
  if (!input.value.trim()) return { ok: false, message: "value is required" };
  return run(() => api.saveRankingPreference({ ...input, value: input.value.trim() }));
}

export async function removeAdjustment(id: string): Promise<RankingResult> {
  return run(() => api.deleteRankingPreference(id));
}
