"use server";

import { revalidatePath } from "next/cache";
import { ApiError, api } from "@/lib/api";

export interface SubmitResult {
  ok: boolean;
  message: string;
}

/**
 * Record that the owner sent an application by hand.
 *
 * Same reason as `swipe/actions.ts`: the queue is a Client Component, and a
 * POST from the browser to the API is cross-origin and preflighted. It runs
 * here so the request originates from the Next server, where `lib/api.ts`
 * says it must.
 *
 * This does not submit anything. It records that the owner did — §2.5 makes
 * the last click theirs, and this is only the note that it happened.
 */
export async function recordSubmitted(applicationId: string): Promise<SubmitResult> {
  try {
    await api.markSubmitted(applicationId);
  } catch (error) {
    if (error instanceof ApiError) return { ok: false, message: error.message };
    throw error;
  }

  revalidatePath("/finish");
  revalidatePath("/");
  return { ok: true, message: "recorded" };
}
